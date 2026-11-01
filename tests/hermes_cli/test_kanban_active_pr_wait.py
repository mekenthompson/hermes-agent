"""A ready card whose PR is already open must not spin in ``ready``.

Before: the dispatcher re-checked such a card every tick, refused it with
``active_pr`` and appended one ``respawn_guarded`` event per tick (~1000 rows
over a few hours) while the board said "Ready for 15h with no worker". An
explicit ``kanban unblock`` was ignored by the guard, and a ``ready`` card
cannot be unblocked at all, so nobody could tell it to go.

Now: the first refusal parks the card as blocked (``needs_input``) with one
plain reason, "waiting for PR #N to merge". An explicit unblock lets exactly
one run through; after that run the guard holds again, so the duplicate-PR
protection still stands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PR_COMMENT = "Opened https://github.com/example/repo/pull/44 for review."


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {})
    kb.init_db()
    return home


def _spawned():
    calls: list[str] = []

    def spawn(task, workspace, board=None):
        calls.append(task.id)

    return calls, spawn


def _events(conn, tid, kind):
    return [json.loads(r["payload"] or "null") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind))]


def _status(conn, tid):
    return conn.execute("SELECT status, block_kind FROM tasks WHERE id = ?", (tid,)).fetchone()


def test_guarded_card_parks_visibly_instead_of_spinning_in_ready(kanban_home):
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already PRed", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)

        for _ in range(5):
            res = kbd.dispatch_once(conn, spawn_fn=spawn)

        assert calls == []
        row = _status(conn, tid)
        assert (row["status"], row["block_kind"]) == ("blocked", "needs_input")
        blocked = _events(conn, tid, "blocked")
        assert [e["reason"] for e in blocked] == ["waiting for PR #44 to merge"]
        # One guard record, not one per tick.
        assert len(_events(conn, tid, "respawn_guarded")) <= 1
        # A blocked card is out of the ready lane: later ticks never see it.
        assert tid not in dict(res.respawn_guarded)


def test_dry_run_reports_the_guard_without_parking(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already PRed", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        res = kbd.dispatch_once(conn, dry_run=True)
        assert dict(res.respawn_guarded).get(tid) == "active_pr"
        assert _status(conn, tid)["status"] == "ready"


def test_explicit_unblock_lets_one_run_through_then_guard_holds(kanban_home):
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already PRed", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert _status(conn, tid)["status"] == "blocked"

        # Ken (or an authorized chat) says go: the guard must not refuse it.
        assert kb.unblock_task(conn, tid)
        assert kbd.check_respawn_guard(conn, tid) is None
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]

        # That run ends without finishing (crash/reclaim) and the card is back
        # in ready: the unblock was spent, so the duplicate-PR guard holds again.
        run_id = kb.get_task(conn, tid).current_run_id
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET outcome='crashed', status='failed', ended_at=? WHERE id=?",
                (kb.get_task(conn, tid).created_at, run_id))
            conn.execute(
                "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL WHERE id=?", (tid,))
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]
        assert _status(conn, tid)["status"] in ("blocked", "triage")


def test_unblock_with_a_pr_comment_in_the_same_second_still_lifts(kanban_home):
    """``kanban unblock <id> "<reason>"`` writes its comment just before the
    unblock; a reason that cites the PR must not re-arm the guard."""
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already PRed", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        kb.add_comment(conn, tid, author="ken", body="UNBLOCK: fix review on " + PR_COMMENT)
        assert kb.unblock_task(conn, tid)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]


def test_guard_still_prevents_a_duplicate_spawn_without_an_unblock(kanban_home):
    """No unblock, no handoff: the worker that opened the PR is never re-spawned
    against it, even after an old unblock that a later run already consumed."""
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="worked once", assignee="dev")
        kb.block_task(conn, tid, reason="need creds", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        claimed = kb.claim_task(conn, tid)  # the run the unblock authorised
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET outcome='crashed', status='failed', ended_at=? WHERE id=?",
                (claimed.created_at, claimed.current_run_id))
            conn.execute(
                "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL WHERE id=?", (tid,))
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == []


def test_other_guard_reasons_are_recorded_once_not_every_tick(kanban_home, monkeypatch):
    calls, spawn = _spawned()
    monkeypatch.setattr(kbd, "check_respawn_guard", lambda *_a, **_k: "rate_limit_cooldown")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="cooling down", assignee="dev")
        for _ in range(4):
            kbd.dispatch_once(conn, spawn_fn=spawn)
        assert _status(conn, tid)["status"] == "ready"
        assert _events(conn, tid, "respawn_guarded") == [{"reason": "rate_limit_cooldown"}]
