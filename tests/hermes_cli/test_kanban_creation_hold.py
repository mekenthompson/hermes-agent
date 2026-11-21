"""A transient creation hold is board state, not a block someone must answer (HF-788).

An integration (the Linear bridge) creates a card ``initial_status="blocked"`` on
purpose, confirms it, and lifts the hold itself within a second. Core wrote the
same ``blocked`` event it writes for a card a person parks, so every consumer —
the gateway notifier and the Desktop toast — reported "Blocked" for it. The
creator now says the hold is transient (``transient_hold=True``); the event keeps
its ``reason: initial_status`` (bridges identify the pristine create+hold pair by
it) and gains ``source: creation_hold``. A card a person creates blocked has no
source and still alerts.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_swarm
from hermes_cli.kanban_db_guard_wait import is_board_hold_event


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _blocked_events(conn, task_id):
    return [ev for ev in kb.list_events(conn, task_id) if ev.kind == "blocked"]


def _notice(conn, task_id, platform="telegram", chat_id="ken"):
    """The notifier's delivery for ``task_id``: what it would ping and wake."""
    from gateway.kanban_watchers_notifier import TERMINAL_KINDS, _KanbanNotification

    kbn.add_notify_sub(conn, task_id=task_id, platform=platform, chat_id=chat_id, delivery_mode="notify+wake")
    # A new subscription starts caught up; a creator's inherited one hears the creation events.
    kbn.advance_notify_cursor(conn, task_id=task_id, platform=platform, chat_id=chat_id, new_cursor=0)
    sub = kbn.list_notify_subs(conn, task_id)[0]
    old, cursor, events = kbn.claim_unseen_events_for_sub(
        conn, task_id=task_id, platform=platform, chat_id=chat_id, kinds=TERMINAL_KINDS)
    task = kb.get_task(conn, task_id)
    sent: list[str] = []
    notice = _KanbanNotification(
        SimpleNamespace(_kanban_sub_op=lambda *a, **k: True),
        {"sub": sub, "task": task, "events": events, "old_cursor": old, "cursor": cursor, "board": "default"},
        platform_cls=None, sub_fail_counts={})

    async def record(ev, msg):
        sent.append(msg)
        return True

    notice._send_event = record
    asyncio.run(notice._send_pings())
    notice.build_wake_text()
    return sent, notice.wake_kinds


def test_transient_hold_marks_the_event_but_keeps_its_reason(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="linear admission", assignee="dev", created_by="linear",
                             initial_status="blocked", transient_hold=True)
        [event] = _blocked_events(conn, tid)

    assert event.payload["reason"] == "initial_status"  # bridges find the pristine pair by this
    assert event.payload["source"] == "creation_hold"
    assert is_board_hold_event(event.kind, event.payload)


def test_a_card_a_person_creates_blocked_is_not_a_board_hold(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="needs sign-off", assignee="dev", created_by="user",
                             initial_status="blocked")
        [event] = _blocked_events(conn, tid)

    assert event.payload == {"reason": "initial_status", "status": "blocked", "actor": "user"}
    assert not is_board_hold_event(event.kind, event.payload)


def test_transient_hold_needs_a_blocked_creation(kanban_home):
    with kbc.connect() as conn, pytest.raises(ValueError, match="transient_hold"):
        kb.create_task(conn, title="not a hold", assignee="dev", transient_hold=True)


def test_the_hold_neither_pings_nor_wakes_but_a_deliberate_block_does(kanban_home):
    with kbc.connect() as conn:
        held = kb.create_task(conn, title="held", assignee="dev", created_by="linear",
                              initial_status="blocked", transient_hold=True)
        deliberate = kb.create_task(conn, title="deliberate", assignee="dev", created_by="user",
                                    initial_status="blocked")
        held_sent, held_wake = _notice(conn, held)
        deliberate_sent, deliberate_wake = _notice(conn, deliberate)

    assert held_sent == [] and held_wake == set()
    assert len(deliberate_sent) == 1 and deliberate_wake == {"blocked"}


def test_lifting_the_hold_leaves_a_guarded_unblock_working(kanban_home):
    """The marker must not disturb the bridge's token: the hold is the newest event."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="admit", assignee="dev", created_by="linear",
                             initial_status="blocked", transient_hold=True)
        [event] = _blocked_events(conn, tid)
        assert kb.unblock_task(conn, tid, expected_event_id=event.id, request_id="linear:admit")
        assert kb.get_task(conn, tid).status == "ready"


def test_the_swarm_planning_root_is_a_transient_hold(kanban_home):
    """The swarm root is blocked only until create_swarm completes it inline."""
    with kbc.connect() as conn:
        created = kanban_swarm.create_swarm(
            conn, goal="compare two options",
            workers=[kanban_swarm.SwarmWorkerSpec(profile="dev", title="research", body="look it up")],
            verifier_assignee="dev", synthesizer_assignee="dev")
        [event] = _blocked_events(conn, created.root_id)

    assert is_board_hold_event(event.kind, event.payload)
