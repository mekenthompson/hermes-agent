"""Canonical Desktop local sessions receive passive checkpoint events."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from gateway.config import Platform
from gateway.kanban_watchers_notifier import TERMINAL_KINDS, _KanbanNotification
from gateway.session import SessionSource
from gateway.session_events import SessionEvents
from gateway.session_local import LocalSessionAdapter
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


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
    authority = SimpleNamespace(sessions={"local-session": live}, runner=None)
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
    adapter = LocalSessionAdapter(SimpleNamespace(sessions={"local-later": live}, runner=None))
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
    replay = live.event_stream.since(live.event_stream.epoch, 0)
    checkpoints = [frame for frame in replay["events"] if frame["type"] == "notification.show"]
    assert checkpoints[-1]["payload"]["detail"] == "visible after attach"
