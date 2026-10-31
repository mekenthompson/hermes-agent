"""Canonical Desktop local sessions receive passive checkpoint events."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from unittest.mock import patch

from gateway.config import Platform
from gateway.kanban_watchers_notifier import TERMINAL_KINDS, _KanbanNotification
from gateway.session import SessionSource
from gateway.session_events import SessionEvents
from gateway.session_local import LocalSessionAdapter
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_state import SessionDB
from hermes_cli.web_routers.sessions import _project_for_display
from gateway.session_controls import display_projection
from hermes_state_checkpoint import persist_local_checkpoint


def test_canonical_desktop_stream_gets_exact_checkpoint_without_model(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb._INITIALIZED_PATHS.clear()
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="Canonical update")
        kbn.add_notify_sub(conn, task_id=task_id, platform="local", chat_id="local-session")
        body = "\n# Canonical\n\n" + ("- exact line\n" * 75) + "\n"
        kb.add_comment(conn, task_id, "worker", body, human_checkpoint=True, request_id="local-1")
        sub = kbn.list_notify_subs(conn, task_id)[0]
        old, cursor, events = kbn.claim_unseen_events_for_sub(
            conn, task_id=task_id, platform="local", chat_id="local-session", kinds=TERMINAL_KINDS)
        assert [event.kind for event in events] == ["checkpoint"]
        task = kb.get_task(conn, task_id)

    source = SessionSource(platform=Platform.LOCAL, chat_id="local-session", user_id="person")
    live = SimpleNamespace(source=source, event_stream=SessionEvents())
    seen = []
    live.event_stream.observers.add(lambda frame: seen.append(frame))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("local-session", "local")
    authority = SimpleNamespace(sessions={"local-session": live}, runner=None, db=db)
    adapter = LocalSessionAdapter(authority)
    adapter.register_source(source)
    runner = SimpleNamespace(_kanban_sub_op=Mock(return_value=True),
                             _deliver_kanban_artifacts=AsyncMock())
    notice = _KanbanNotification(runner, {"sub": sub, "task": task, "events": events,
                                           "old_cursor": old, "cursor": cursor, "board": "default"},
                                 platform_cls=None, sub_fail_counts={})
    notice.adapter = adapter
    assert asyncio.run(notice._send_pings()) is True
    replay = live.event_stream.since(live.event_stream.epoch, 0)
    checkpoint_events = [frame for frame in replay["events"]
                         if frame["type"] == "notification.show"]
    assert len(checkpoint_events) == 1
    assert len(seen) == 1
    assert checkpoint_events[0]["payload"]["detail"] == body
    notice.build_wake_text()
    assert not notice.wake_kinds
    # A cold gateway cannot replay its former event ring. The same checkpoint
    # must remain in the originating conversation's stored display history.
    live.event_stream = SessionEvents()
    db.close()
    reopened = SessionDB(db_path=tmp_path / "state.db")
    stored = display_projection(reopened.get_messages("local-session"), None)
    assert [row["text"] for row in stored if row.get("display_kind") == "kanban_checkpoint"] == [body]
    desktop = _project_for_display(reopened.get_messages("local-session"))
    assert [(row["role"], row["content"]) for row in desktop
            if row.get("display_kind") == "kanban_checkpoint"] == [("system", body)]
    model_history, _ = reopened.get_resume_conversations("local-session")
    assert all(body not in str(row.get("content")) for row in model_history)
    assert all(body not in str(row.get("content")) for row in
               reopened.get_messages_as_conversation("local-session"))
    reopened.close()


def test_canonical_desktop_without_viewer_retries_when_viewer_attaches(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb._INITIALIZED_PATHS.clear()
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="Later viewer")
        kbn.add_notify_sub(conn, task_id=task_id, platform="local", chat_id="local-later")
        kb.add_comment(conn, task_id, "worker", "visible after attach",
                       human_checkpoint=True, request_id="local-later-1")

    source = SessionSource(platform=Platform.LOCAL, chat_id="local-later", user_id="person")
    live = SimpleNamespace(source=source, event_stream=SessionEvents())
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("local-later", "local")
    adapter = LocalSessionAdapter(SimpleNamespace(sessions={"local-later": live}, runner=None, db=db))
    adapter.register_source(source)

    def sub_op(_board, action, sub, **kwargs):
        with kbc.connect_closing() as writer:
            return getattr(kbn, action)(writer, task_id=sub["task_id"], platform=sub["platform"],
                                        chat_id=sub["chat_id"], thread_id=sub.get("thread_id"), **kwargs)

    def rewind(sub, cursor, old, _board):
        return sub_op("default", "rewind_notify_cursor", sub, claimed_cursor=cursor, old_cursor=old)

    runner = SimpleNamespace(_kanban_sub_op=sub_op, _kanban_rewind=rewind,
                             _deliver_kanban_artifacts=AsyncMock())

    def attempt():
        with kbc.connect_closing() as reader:
            sub = kbn.list_notify_subs(reader, task_id)[0]
            old, cursor, events = kbn.claim_unseen_events_for_sub(
                reader, task_id=task_id, platform="local", chat_id="local-later", kinds=TERMINAL_KINDS)
            notice = _KanbanNotification(runner, {"sub": sub, "task": kb.get_task(reader, task_id),
                                                   "events": events, "old_cursor": old, "cursor": cursor,
                                                   "board": "default"}, platform_cls=None, sub_fail_counts={})
            notice.adapter = adapter
            return asyncio.run(notice._send_pings())

    assert attempt() is False
    with kbc.connect_closing() as reader:
        assert kbn.list_notify_subs(reader, task_id)[0]["checkpoint_pending_event_ids"] == []
    seen = []
    live.event_stream.observers.add(lambda frame: seen.append(frame))
    assert attempt() is True
    assert len(seen) == 1
    assert len([row for row in db.get_messages("local-later")
                if row.get("display_kind") == "kanban_checkpoint"]) == 1
    replay = live.event_stream.since(live.event_stream.epoch, 0)
    checkpoints = [frame for frame in replay["events"] if frame["type"] == "notification.show"]
    assert checkpoints[-1]["payload"]["detail"] == "visible after attach"


def test_checkpoint_retry_after_compression_keeps_one_display_row(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("origin", "local")
    key = "kanban-checkpoint:default:task:9"
    first = persist_local_checkpoint(db, "origin", key, "## Progress\n\nVerified.",
                                     board="default", task_id="task")
    db.end_session("origin", "compression")
    db.create_session("continuation", "local", parent_session_id="origin")
    assert db.resolve_resume_session_id("origin") == "continuation"
    assert persist_local_checkpoint(db, "origin", key, "## Progress\n\nVerified.",
                                    board="default", task_id="task") == first
    assert len([row for row in db.get_messages("continuation", include_ancestors=True)
                if row.get("display_kind") == "kanban_checkpoint"]) == 1


def test_local_checkpoint_storage_failure_is_known_unsent(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("origin", "local")
    source = SessionSource(platform=Platform.LOCAL, chat_id="origin", user_id="person")
    live = SimpleNamespace(source=source, event_stream=SessionEvents())
    live.event_stream.observers.add(lambda _frame: None)
    adapter = LocalSessionAdapter(SimpleNamespace(sessions={"origin": live}, runner=None, db=db))
    adapter.register_source(source)
    metadata = {"kanban_checkpoint_event_id": 7, "kanban_checkpoint_task_id": "task",
                "kanban_checkpoint_board": "default"}
    with patch("hermes_state_checkpoint.persist_local_checkpoint", side_effect=OSError("scratch disk refused")):
        result = asyncio.run(adapter.send("origin", "Visible progress", metadata=metadata))
    assert result.success is False
    assert live.event_stream.sequence == 0
    assert db.get_messages("origin") == []
    assert asyncio.run(adapter.send("origin", "Visible progress", metadata=metadata)).success is True
