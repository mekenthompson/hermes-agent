"""A ready card whose PR is already open must not spin in ``ready``.

Before: the dispatcher re-checked such a card every tick, refused it with
``active_pr`` and appended one ``respawn_guarded`` event per tick (~1000 rows
over a few hours) while the board said "Ready for 15h with no worker". An
explicit ``kanban unblock`` was ignored by the guard, and a ``ready`` card
cannot be unblocked at all, so nobody could tell it to go.

Now: the first refusal parks the card as blocked (``needs_input``) with one
plain reason, "waiting for PR #N to merge". An explicit unblock lets exactly
one run through; after that run the guard holds again, so the duplicate-PR
protection still stands. The park releases itself exactly when the guard
would have let a ready card go: the PR comment ages out of the 24h window, or
the card is handed to another profile.
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
        row = _status(conn, tid)
        assert (row["status"], row["block_kind"]) == ("blocked", "needs_input")
        assert [e["reason"] for e in _events(conn, tid, "blocked")] == [
            "waiting for PR #44 to merge"] * 2
        assert _events(conn, tid, "block_loop_detected") == []


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
    _calls, spawn = _spawned()
    monkeypatch.setattr(kbd, "check_respawn_guard", lambda *_a, **_k: "rate_limit_cooldown")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="cooling down", assignee="dev")
        for _ in range(4):
            kbd.dispatch_once(conn, spawn_fn=spawn)
        assert _status(conn, tid)["status"] == "ready"
        assert _events(conn, tid, "respawn_guarded") == [{"reason": "rate_limit_cooldown"}]


def _crash_after_pr(conn, tid):
    """The worker opened the PR, then crashed / was reclaimed: back in ready."""
    claimed = kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET outcome='crashed', status='failed', ended_at=? WHERE id=?",
            (claimed.created_at, claimed.current_run_id))
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL WHERE id=?", (tid,))


def test_park_after_an_earlier_needs_input_block_is_not_a_block_loop(kanban_home):
    """A card that once asked Ken a question, was unblocked, opened its PR and
    crashed must park as "waiting for PR #N to merge", not drop to triage via
    the block-loop breaker (where ``unblock`` can no longer reach it)."""
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="asked once", assignee="dev")
        kb.block_task(conn, tid, reason="which repo?", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        _crash_after_pr(conn, tid)

        for _ in range(3):
            kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == []
        row = _status(conn, tid)
        assert (row["status"], row["block_kind"]) == ("blocked", "needs_input")
        assert _events(conn, tid, "block_loop_detected") == []
        assert _events(conn, tid, "blocked")[-1]["reason"] == "waiting for PR #44 to merge"

        # The explicit resume still works from here: exactly one run goes.
        assert kb.unblock_task(conn, tid)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]


def test_park_leaves_the_block_loop_counter_as_it_was(kanban_home):
    """The dispatcher's park is not a worker asking for input: it neither
    counts toward nor erases the same-reason block-loop history."""
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fresh", assignee="dev")
        _crash_after_pr(conn, tid)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert kb.unblock_task(conn, tid)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]
        # The resumed worker genuinely needs input: its first real ask parks it.
        assert kb.block_task(conn, tid, reason="need creds", kind="needs_input")
        assert _status(conn, tid)["status"] == "blocked"
        # A real repeat of the same ask is still caught by the breaker.
        assert kb.unblock_task(conn, tid)
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="need creds", kind="needs_input")
        assert _status(conn, tid)["status"] == "triage"


def _age_pr_comment(conn, tid, seconds):
    with kb.write_txn(conn):
        conn.execute("UPDATE task_comments SET created_at = created_at - ? WHERE task_id = ?", (seconds, tid))


def _spawned_as():
    calls: list[tuple[str, str]] = []

    def spawn(task, workspace, board=None):
        calls.append((task.id, task.assignee))

    return calls, spawn


def test_park_releases_itself_when_the_pr_window_runs_out(kanban_home):
    """Before the park existed, the guard stopped refusing once the PR comment
    was older than the 24h window and the card ran by itself (the worker saw
    the merged PR and finished). The park must keep that self-recovery."""
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already PRed", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert _status(conn, tid)["status"] == "blocked"

        _age_pr_comment(conn, tid, kbd._RESPAWN_GUARD_PR_WINDOW + 3600)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]
        assert _status(conn, tid)["status"] == "running"
        assert _events(conn, tid, "promoted") == [{"source": "respawn_guard"}]
        # The release is not an explicit unblock: it grants no one-run consent.
        assert _events(conn, tid, "unblocked") == []


def test_handing_a_parked_card_to_another_profile_runs_it(kanban_home):
    """An operator reassign after the PR comment is a deliberate handoff (a
    closer finishing that PR, #111910). It must work from the parked state."""
    calls, spawn = _spawned_as()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already PRed", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        _age_pr_comment(conn, tid, 10)  # the handoff lands strictly after it
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert _status(conn, tid)["status"] == "blocked"

        assert kb.assign_task(conn, tid, "closer")
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [(tid, "closer")]
        assert _status(conn, tid)["status"] == "running"


def test_parked_card_stays_parked_while_the_guard_holds(kanban_home):
    """No expiry, no handoff: a same-profile re-assign does not release it."""
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already PRed", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        _age_pr_comment(conn, tid, 10)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert kb.assign_task(conn, tid, "dev")
        for _ in range(3):
            kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == []
        assert _status(conn, tid)["status"] == "blocked"
        assert _events(conn, tid, "promoted") == []


def test_a_human_stop_on_a_parked_card_is_not_released_automatically(kanban_home):
    """Only the dispatcher's own park self-releases; a human block wins."""
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already PRed", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        last = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
        assert kb.block_task(conn, tid, reason="hold this one", kind="needs_input", expected_event_id=last)

        _age_pr_comment(conn, tid, kbd._RESPAWN_GUARD_PR_WINDOW + 3600)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == []
        assert _status(conn, tid)["status"] == "blocked"


def test_a_released_park_does_not_make_a_later_block_sticky(kanban_home):
    """After the release, the park's ``blocked`` event must not keep a later
    non-sticky block (e.g. a breaker trip) from auto-recovering."""
    _calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already PRed", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert kb._has_sticky_block(conn, tid)
        _age_pr_comment(conn, tid, kbd._RESPAWN_GUARD_PR_WINDOW + 3600)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert not kb._has_sticky_block(conn, tid)


def test_park_sends_no_stop_to_earlier_owners_and_the_tick_goes_on(kanban_home, monkeypatch):
    """The park has no running work to stop. A card with an earlier admitted
    owner must not get a native Stop delivered (whose failure would escape
    ``dispatch_once`` and starve the rest of the board's tick)."""
    from types import SimpleNamespace

    delivered = []
    monkeypatch.setattr("hermes_cli.gateway_runtime.discover_gateway_endpoint",
                        lambda home: delivered.append(home) or SimpleNamespace(state="stopped", endpoint=None))
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already PRed", assignee="dev")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "owner_admitted", {
                "db": str(kanban_home / "other-profile" / "state.db"),
                "session_id": "earlier", "request_id": "run-1"})
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        other = kb.create_task(conn, title="independent work", assignee="dev")

        kbd.dispatch_once(conn, spawn_fn=spawn)

        assert delivered == []
        row = _status(conn, tid)
        assert (row["status"], row["block_kind"]) == ("blocked", "needs_input")
        assert calls == [other]
