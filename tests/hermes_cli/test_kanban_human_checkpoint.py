"""Explicit human checkpoints are durable, exact, and idempotent."""
import json
import asyncio
from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from gateway.kanban_watchers_notifier import TERMINAL_KINDS, _KanbanNotification
from gateway.session_context import clear_session_vars, set_session_vars
from tools import kanban_tools  # registers the real producer
from tools.registry import registry


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "worker")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb._INITIALIZED_PATHS.clear()
    conn = kbc.connect()
    task_id = kb.create_task(conn, title="Checkpoint contract")
    yield conn, task_id
    conn.close()


def test_registered_comment_producer_persists_exact_checkpoint_once(board):
    conn, task_id = board
    body = "\n# Update\n\n" + ("- exact Markdown line\n" * 60) + "\n"
    args = {"task_id": task_id, "body": body, "human_checkpoint": True,
            "request_id": "checkpoint-001"}
    first = json.loads(registry.dispatch("kanban_comment", args))
    second = json.loads(registry.dispatch("kanban_comment", args))
    assert first["comment_id"] == second["comment_id"]
    conn.close()
    reopened = kbc.connect()
    try:
        comments = kb.list_comments(reopened, task_id)
        events = kb.list_events(reopened, task_id)
        assert len(comments) == 1
        assert comments[0].body == body
        checkpoints = [e for e in events if e.kind == "checkpoint"]
        assert len(checkpoints) == 1
        assert checkpoints[0].payload == {"author": "worker", "body": body,
                                          "comment_id": comments[0].id,
                                          "request_id": "checkpoint-001"}
        conflict = json.loads(registry.dispatch("kanban_comment", {**args, "body": body + "different"}))
        assert "error" in conflict
        assert len(kb.list_comments(reopened, task_id)) == 1
    finally:
        reopened.close()


def test_ordinary_comment_stays_internal(board):
    conn, task_id = board
    kb.add_comment(conn, task_id, "worker", "internal note")
    events = kb.list_events(conn, task_id)
    assert [e.kind for e in events if e.kind in {"commented", "checkpoint"}] == ["commented"]


def test_checkpoint_reaches_passive_notifier_without_wake(board):
    conn, task_id = board
    kbn.add_notify_sub(conn, task_id=task_id, platform="slack", chat_id="C1",
                       thread_id="T1", delivery_mode="notify+wake")
    body = "\n## Human update\n\n" + ("- one line\n" * 90) + "\n"
    produced = json.loads(registry.dispatch("kanban_comment", {
        "task_id": task_id, "body": body, "human_checkpoint": True, "request_id": "cp-2"}))
    assert produced.get("comment_id")
    sub = kbn.list_notify_subs(conn)[0]
    old, cursor, events = kbn.claim_unseen_events_for_sub(
        conn, task_id=task_id, platform="slack", chat_id="C1", thread_id="T1", kinds=TERMINAL_KINDS)
    assert [event.kind for event in events] == ["checkpoint"]
    adapter = SimpleNamespace(send=AsyncMock(return_value=None))
    runner = SimpleNamespace(_kanban_sub_op=Mock(), _deliver_kanban_artifacts=AsyncMock())
    notice = _KanbanNotification(runner, {"sub": sub, "task": kb.get_task(conn, task_id),
                                           "events": events, "old_cursor": old, "cursor": cursor,
                                           "board": "default"}, platform_cls=None, sub_fail_counts={})
    notice.adapter = adapter
    assert asyncio.run(notice._send_pings()) is True
    assert adapter.send.await_args.args[1] == body
    notice.build_wake_text()
    assert not notice.wake_kinds


def test_guarded_resume_subscribes_requester_only_on_success(board):
    conn, task_id = board
    kbn.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="older",
                       thread_id="topic", delivery_mode="notify")
    assert kb.block_task(conn, task_id, reason="needs decision")
    observed = kb.list_events(conn, task_id)[-1].id
    target = {"platform": "slack", "chat_id": "C2", "thread_id": "T2",
              "user_id": "U2", "notifier_profile": "worker", "delivery_mode": "notify+wake",
              "delivery_metadata": {"scope_id": "W2", "parent_chat_id": "C2"}}
    assert not kb.unblock_task(conn, task_id, expected_event_id=observed - 1,
                               notify_target=target)
    assert len(kbn.list_notify_subs(conn)) == 1
    assert kb.unblock_task(conn, task_id, expected_event_id=observed, notify_target=target)
    subs = kbn.list_notify_subs(conn)
    assert len(subs) == 2
    assert next(s for s in subs if s["chat_id"] == "older")["delivery_mode"] == "notify"
    new = next(s for s in subs if s["chat_id"] == "C2")
    assert (new["thread_id"], new["user_id"], new["notifier_profile"]) == ("T2", "U2", "worker")
    assert new["delivery_metadata"]["scope_id"] == "W2"


def test_response_loss_after_remote_apply_is_held_across_restart(board):
    conn, task_id = board
    kbn.add_notify_sub(conn, task_id=task_id, platform="slack", chat_id="C3", delivery_mode="notify")
    kb.add_comment(conn, task_id, "worker", "sent once", human_checkpoint=True, request_id="cp-3")
    remote = []

    def sub_op(_board, action, sub, **kwargs):
        with kbc.connect_closing() as writer:
            return getattr(kbn, action)(writer, task_id=sub["task_id"], platform=sub["platform"],
                                        chat_id=sub["chat_id"], thread_id=sub.get("thread_id"), **kwargs)

    def rewind(sub, cursor, old, _board):
        return sub_op("default", "rewind_notify_cursor", sub, claimed_cursor=cursor, old_cursor=old)

    async def applied_then_lost(chat, message, **kwargs):
        remote.append(message)
        raise ConnectionError("response lost after remote apply")

    def attempt():
        with kbc.connect_closing() as reader:
            sub = kbn.list_notify_subs(reader, task_id)[0]
            old, cursor, events = kbn.claim_unseen_events_for_sub(
                reader, task_id=task_id, platform="slack", chat_id="C3", kinds=TERMINAL_KINDS)
            runner = SimpleNamespace(_kanban_sub_op=sub_op, _kanban_rewind=rewind,
                                     _deliver_kanban_artifacts=AsyncMock())
            notice = _KanbanNotification(runner, {"sub": sub, "task": kb.get_task(reader, task_id),
                                                   "events": events, "old_cursor": old,
                                                   "cursor": cursor, "board": "default"},
                                         platform_cls=None, sub_fail_counts={})
            notice.adapter = SimpleNamespace(send=AsyncMock(side_effect=applied_then_lost))
            return asyncio.run(notice._send_pings())

    assert attempt() is False
    with kbc.connect_closing() as writer:
        other_task = kb.create_task(writer, title="Independent update")
        kbn.add_notify_sub(writer, task_id=other_task, platform="telegram", chat_id="independent")
        kb.add_comment(writer, other_task, "worker", "other subscriber progresses",
                       human_checkpoint=True, request_id="cp-other")
        other_sub = kbn.list_notify_subs(writer, other_task)[0]
        other_old, other_cursor, other_events = kbn.claim_unseen_events_for_sub(
            writer, task_id=other_task, platform="telegram", chat_id="independent", kinds=TERMINAL_KINDS)
        other_notice = _KanbanNotification(
            SimpleNamespace(_kanban_sub_op=sub_op, _deliver_kanban_artifacts=AsyncMock()),
            {"sub": other_sub, "task": kb.get_task(writer, other_task), "events": other_events,
             "old_cursor": other_old, "cursor": other_cursor, "board": "default"},
            platform_cls=None, sub_fail_counts={})
        other_notice.adapter = SimpleNamespace(send=AsyncMock(return_value=None))
        assert asyncio.run(other_notice._send_pings()) is True
        other_notice.adapter.send.assert_awaited_once()
    assert attempt() is True
    assert remote == ["sent once"]
    with kbc.connect_closing() as reader:
        sub = kbn.list_notify_subs(reader, task_id)[0]
        assert kb.list_events(reader, task_id)[-1].id in sub["checkpoint_pending_event_ids"]


@pytest.mark.parametrize("platform,chat,thread,session_key,expected_platform", [
    ("slack", "C4", "T4", "", "slack"),
    ("telegram", "42", "topic-4", "", "telegram"),
    ("", "", "", "desktop-4", "tui"),
])
def test_real_unblock_tool_subscribes_exact_origin(board, platform, chat, thread, session_key, expected_platform):
    conn, task_id = board
    assert kb.block_task(conn, task_id, reason="human decision")
    observed = kb.list_events(conn, task_id)[-1].id
    tokens = set_session_vars(platform=platform, chat_id=chat, thread_id=thread,
                              user_id="person-4", profile="worker", scope_id="workspace-4",
                              parent_chat_id=chat, session_key=session_key)
    try:
        out = json.loads(registry.dispatch("kanban_unblock", {
            "task_id": task_id, "expected_event_id": observed}))
        assert out.get("ok") is True, out
    finally:
        clear_session_vars(tokens)
    subs = kbn.list_notify_subs(conn, task_id)
    assert len(subs) == 1
    assert (subs[0]["platform"], subs[0]["chat_id"], subs[0]["thread_id"]) == (
        expected_platform, chat or session_key, thread)
    assert subs[0]["notifier_profile"] == "worker"


def test_foreign_session_profile_cannot_attach_to_resume(board):
    conn, task_id = board
    assert kb.block_task(conn, task_id, reason="human decision")
    observed = kb.list_events(conn, task_id)[-1].id
    tokens = set_session_vars(platform="slack", chat_id="foreign", profile="another-profile")
    try:
        out = json.loads(registry.dispatch("kanban_unblock", {
            "task_id": task_id, "expected_event_id": observed}))
        assert "error" in out
    finally:
        clear_session_vars(tokens)
    assert kb.get_task(conn, task_id).status == "blocked"
    assert kbn.list_notify_subs(conn, task_id) == []


def test_cli_resume_captures_requesting_thread_only_after_guarded_success(board):
    from hermes_cli.kanban import _cmd_unblock

    conn, task_id = board
    assert kb.block_task(conn, task_id, reason="human decision")
    observed = kb.list_events(conn, task_id)[-1].id
    tokens = set_session_vars(platform="telegram", chat_id="42", thread_id="topic-cli",
                              user_id="person-cli", profile="worker")
    try:
        args = Namespace(task_ids=[task_id], expected_event_id=observed - 1, reason=None)
        assert _cmd_unblock(args) == 1
        assert kbn.list_notify_subs(conn, task_id) == []
        args.expected_event_id = observed
        assert _cmd_unblock(args) == 0
    finally:
        clear_session_vars(tokens)
    assert [(s["platform"], s["chat_id"], s["thread_id"], s["user_id"])
            for s in kbn.list_notify_subs(conn, task_id)] == [
                ("telegram", "42", "topic-cli", "person-cli")]


def test_known_unsent_checkpoint_retries_through_existing_consumer(board, monkeypatch):
    from gateway import warning_notifications

    conn, task_id = board
    kbn.add_notify_sub(conn, task_id=task_id, platform="slack", chat_id="C5")
    kb.add_comment(conn, task_id, "worker", "known unsent", human_checkpoint=True, request_id="cp-5")
    sub = kbn.list_notify_subs(conn, task_id)[0]
    old, cursor, events = kbn.claim_unseen_events_for_sub(
        conn, task_id=task_id, platform="slack", chat_id="C5", kinds=TERMINAL_KINDS)

    def sub_op(_board, action, row, **kwargs):
        with kbc.connect_closing() as writer:
            return getattr(kbn, action)(writer, task_id=row["task_id"], platform=row["platform"],
                                        chat_id=row["chat_id"], thread_id=row.get("thread_id"), **kwargs)

    runner = SimpleNamespace(_kanban_sub_op=sub_op, _kanban_rewind=Mock(),
                             _deliver_kanban_artifacts=AsyncMock())
    notice = _KanbanNotification(runner, {"sub": sub, "task": kb.get_task(conn, task_id),
                                           "events": events, "old_cursor": old, "cursor": cursor,
                                           "board": "default"}, platform_cls=None, sub_fail_counts={})
    adapter = SimpleNamespace(send=AsyncMock(return_value=None))
    notice.adapter = adapter
    calls = [0]

    async def present(send, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            return False  # The transport was never called.
        await send()
        return True

    monkeypatch.setattr(warning_notifications, "present_notification", present)
    assert asyncio.run(notice._send_pings()) is False
    adapter.send.assert_not_awaited()
    assert asyncio.run(notice._send_pings()) is True
    adapter.send.assert_awaited_once()
    assert kbn.list_notify_subs(conn, task_id)[0]["checkpoint_pending_event_ids"] == []


def test_descendant_and_dependency_keep_authorized_routes_without_pending_receipts(board):
    conn, parent = board
    for platform, chat, thread in (("slack", "C6", "T6"), ("telegram", "42", "topic-6")):
        kbn.add_notify_sub(conn, task_id=parent, platform=platform, chat_id=chat,
                           thread_id=thread, user_id="person-6", delivery_mode="notify")
    kb.add_comment(conn, parent, "worker", "uncertain", human_checkpoint=True, request_id="cp-6")
    checkpoint_id = kb.list_events(conn, parent)[-1].id
    assert kbn.begin_checkpoint_ping(conn, task_id=parent, platform="slack", chat_id="C6",
                                     thread_id="T6", event_id=checkpoint_id)
    child = kb.create_task(conn, title="Descendant", creator_task_id=parent)
    dependency = kb.create_task(conn, title="Dependency target")
    kb.link_tasks(conn, parent, dependency)
    for task_id in (child, dependency):
        inherited = kbn.list_notify_subs(conn, task_id)
        assert {(row["platform"], row["chat_id"], row["thread_id"]) for row in inherited} == {
            ("slack", "C6", "T6"), ("telegram", "42", "topic-6")}
        assert all(row["checkpoint_pending_event_ids"] == [] for row in inherited)


def test_legacy_subscription_migration_keeps_cursors_and_no_history_replay(board):
    conn, task_id = board
    kb.add_comment(conn, task_id, "worker", "older internal note")
    kbn.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="legacy")
    conn.execute("UPDATE kanban_notify_subs SET last_ping_event_id = 7 WHERE task_id = ?", (task_id,))
    before = kbn.list_notify_subs(conn, task_id)[0]
    conn.execute("ALTER TABLE kanban_notify_subs DROP COLUMN checkpoint_pending_event_ids")
    conn.close()
    kb._INITIALIZED_PATHS.clear()
    with kbc.connect_closing() as migrated:
        after = kbn.list_notify_subs(migrated, task_id)[0]
        assert after["last_event_id"] == before["last_event_id"]
        assert after["last_ping_event_id"] == 7
        assert after["checkpoint_pending_event_ids"] == []
        assert kbn.unseen_events_for_sub(migrated, task_id=task_id,
                                         platform="telegram", chat_id="legacy",
                                         kinds=TERMINAL_KINDS)[1] == []
