"""What the dispatcher does with a card the ``active_pr`` respawn guard refuses.

The guard never clears on its own while the PR comment is fresh, so refusing
the card every tick left it spinning in ``ready`` ("Ready for 15h with no
worker") and wrote one ``respawn_guarded`` event per tick. Instead the card is
parked as a sticky ``needs_input`` block with one plain reason, and an explicit
unblock lets exactly one run through. The park is not a worker asking for input,
so it stays out of the block-loop breaker (a card that once asked a question
must not drop to ``triage``, where ``unblock`` cannot reach it).
"""
from __future__ import annotations

import sqlite3
from typing import Optional


def unblock_unspent(conn: sqlite3.Connection, task_id: str) -> bool:
    """An explicit unblock (orchestrator-only: CLI / ``kanban_unblock``) that no
    claim has spent yet: a deliberate "run it anyway" for ONE run. Event ids
    order it, not timestamps, so a PR link in the unblock's own same-second
    comment cannot re-arm the guard."""
    latest = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND kind IN ('unblocked', 'claimed') "
        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return latest is not None and latest["kind"] == "unblocked"


def record_respawn_guard(conn: sqlite3.Connection, task_id: str, reason: str, *, lane: str) -> None:
    """Make a refused spawn visible without writing a row every tick.

    ``active_pr`` on the ready lane parks the card ("waiting for PR #N to
    merge"). Other reasons expire by themselves and stay in ``ready``; their
    event is written only when it differs from the card's latest event, so
    ``hermes kanban tail`` still says why the card waits."""
    from hermes_cli import kanban_db as kb

    if reason == "active_pr" and lane == "ready":
        last = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task_id,)).fetchone()[0]
        pr = _newest_pr_number(conn, task_id)
        why = f"waiting for PR #{pr} to merge" if pr else "waiting for its open PR to merge"
        # The observed event guards the park: any newer event (a claim, a
        # human move) wins and this tick falls back to the plain record.
        if last is not None and kb.block_task(
                conn, task_id, reason=why, kind="needs_input", expected_event_id=last,
                count_recurrence=False):
            return
    payload = {"reason": reason}
    with kb.write_txn(conn):
        last = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        if last is None or last["kind"] != "respawn_guarded" or last["payload"] != kb._json_or_null(payload):
            kb._append_event(conn, task_id, "respawn_guarded", payload)


def _newest_pr_number(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """PR number from the newest comment carrying a GitHub PR URL, if any."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _RESPAWN_GUARD_PR_URL_RE

    for row in conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC", (task_id,)):
        urls = _RESPAWN_GUARD_PR_URL_RE.findall(kb._lossy_text(row["body"]) or "")
        if urls:
            return urls[-1].rsplit("/", 1)[-1]
    return None
