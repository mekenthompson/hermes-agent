"""HF-628: a crash/restart requeue of a task that already has a PR is not a
false ``needs_input`` human blocker; it gets ONE bounded recovery respawn told
to continue the existing PR. Every other ``active_pr`` case keeps the guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PR_URL = "https://github.com/example/repo/pull/933"
PR_COMMENT = f"Opened {PR_URL} for review."


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


def _spawner():
    calls: list[str] = []

    def spawn(task, workspace, board=None):
        calls.append(task.id)

    return calls, spawn


def _end_run_back_to_ready(conn, tid, claimed, outcome, metadata=None, error=None):
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET outcome=?, status='failed', ended_at=?, metadata=?, error=? WHERE id=?",
            (outcome, claimed.created_at, json.dumps(metadata) if metadata else None, error,
             claimed.current_run_id))
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL WHERE id=?", (tid,))


def _opened_pr_then_ended(conn, outcome, **kw):
    tid = kb.create_task(conn, title="has a PR", assignee="dev")
    claimed = kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
    _end_run_back_to_ready(conn, tid, claimed, outcome, **kw)
    return tid


def _status(conn, tid):
    return conn.execute("SELECT status, block_kind FROM tasks WHERE id = ?", (tid,)).fetchone()


def _comments(conn, tid):
    return [r["body"] for r in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (tid,))]


def test_crash_with_pr_url_respawns_with_recovery_note_not_parked(kanban_home):
    calls, spawn = _spawner()
    with kbc.connect() as conn:
        tid = _opened_pr_then_ended(conn, "crashed", error="worker exited 130")
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]
        assert _status(conn, tid)["status"] == "running"
        notes = [c for c in _comments(conn, tid) if "Crash recovery" in c]
        assert len(notes) == 1
        assert PR_URL in notes[0] and "Do NOT open a new PR" in notes[0]
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='blocked'",
                            (tid,)).fetchone()[0] == 0


def test_bypass_is_spent_once_per_crashed_run(kanban_home):
    calls, spawn = _spawner()
    with kbc.connect() as conn:
        tid = _opened_pr_then_ended(conn, "crashed")
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]
        # Same crashed run can't authorise a second respawn (e.g. claim released without a new run end).
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_second_crash_gets_its_own_bypass_but_crash_counting_still_trips(kanban_home):
    calls, spawn = _spawner()
    with kbc.connect() as conn:
        tid = _opened_pr_then_ended(conn, "crashed")
        kbd.dispatch_once(conn, spawn_fn=spawn)
        claimed = kb.get_task(conn, tid)
        # Crash again and let the real breaker account it.
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET outcome='crashed', status='failed', ended_at=? WHERE id=?",
                         (claimed.created_at + 5, claimed.current_run_id))
            conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
                         "claim_expires=NULL, worker_pid=NULL WHERE id=?", (tid,))
        assert kbd._record_task_failure(conn, tid, "boom", outcome="crashed", failure_limit=2) is False
        assert kbd._record_task_failure(conn, tid, "boom", outcome="crashed", failure_limit=2) is True
        assert _status(conn, tid)["status"] == "blocked"
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]  # gave_up block wins; no third spawn


@pytest.mark.parametrize("outcome,meta,error", [
    ("reclaimed", None, None),
    ("timed_out", None, None),
    ("crashed", {"protocol_violation": True}, "worker exited cleanly (rc=0) ... protocol violation."),
])
def test_normal_exit_or_other_outcomes_keep_the_guard(kanban_home, outcome, meta, error):
    calls, spawn = _spawner()
    with kbc.connect() as conn:
        tid = _opened_pr_then_ended(conn, outcome, metadata=meta, error=error)
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == []
        row = _status(conn, tid)
        assert (row["status"], row["block_kind"]) == ("blocked", "needs_input")


def test_handoff_path_unchanged(kanban_home):
    calls, spawn = _spawner()
    with kbc.connect() as conn:
        tid = _opened_pr_then_ended(conn, "reclaimed")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET created_at = created_at - 10 WHERE task_id = ?", (tid,))
        kb.assign_task(conn, tid, "reviewer")
        assert kbd.check_respawn_guard(conn, tid) is None
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]
        assert not [c for c in _comments(conn, tid) if "Crash recovery" in c]


def test_unreadable_comment_time_fails_closed(kanban_home):
    with kbc.connect() as conn:
        tid = _opened_pr_then_ended(conn, "crashed")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET created_at='not a time' WHERE task_id=?", (tid,))
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_dry_run_does_not_spend_bypass_or_comment(kanban_home):
    with kbc.connect() as conn:
        tid = _opened_pr_then_ended(conn, "crashed")
        kbd.dispatch_once(conn, dry_run=True)
        assert not [c for c in _comments(conn, tid) if "Crash recovery" in c]
        assert kbd.check_respawn_guard(conn, tid) is None


def test_blocked_for_other_reason_is_not_respawned(kanban_home):
    calls, spawn = _spawner()
    with kbc.connect() as conn:
        tid = _opened_pr_then_ended(conn, "crashed")
        kb.block_task(conn, tid, reason="Ken says stop", kind="needs_input")
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == []
        assert _status(conn, tid)["status"] == "blocked"
