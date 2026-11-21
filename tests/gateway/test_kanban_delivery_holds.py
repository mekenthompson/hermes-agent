"""Failed adapter receipts retain notification custody and the original reply anchor."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.kanban_watchers_notifier import _KanbanNotification
from gateway.platforms.base import SendResult


def notification(adapter):
    runner = SimpleNamespace(
        _kanban_rewind=Mock(), _kanban_advance=Mock(), _kanban_unsub=Mock(),
        _kanban_sub_op=Mock(), _deliver_kanban_artifacts=AsyncMock(),
    )
    event = SimpleNamespace(id=7, kind="completed", payload={"summary": "done"})
    sub = {"task_id": "task", "platform": "linear", "chat_id": "issue",
           "thread_id": "session", "delivery_mode": "notify", "last_ping_event_id": 0,
           "delivery_metadata": {"reply_to": "activity"}}
    n = _KanbanNotification(runner, {"sub": sub, "task": None, "events": [event],
                                      "cursor": 7, "old_cursor": 2, "board": "default"},
                            platform_cls=None, sub_fail_counts={})
    n.adapter = adapter
    return n, event


def test_bound_reply_anchor_reaches_adapter_without_inventing_one():
    adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True)))
    n, event = notification(adapter)
    assert asyncio.run(n._send_event(event, "done"))
    assert adapter.send.call_args.kwargs["reply_to"] == "activity"
    n.sub["delivery_metadata"] = {}
    assert asyncio.run(n._send_event(event, "done"))
    assert "reply_to" not in adapter.send.call_args.kwargs


@pytest.mark.parametrize("retryable", [False, True])
def test_failed_receipts_hold_subscription_and_cursors_beyond_retry_limit(retryable, caplog, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(
        success=False, error="source authorization held", retryable=retryable)))
    n, _ = notification(adapter)
    for attempt in range(15):
        assert asyncio.run(n._send_pings()) is False
        assert adapter.send.await_count == attempt + 1
        # A poll in the backoff window retains custody without another send.
        assert asyncio.run(n._send_pings()) is False
        assert adapter.send.await_count == attempt + 1
        clock[0] += 301
    n.runner._kanban_unsub.assert_not_called()
    n.runner._kanban_advance.assert_not_called()
    n.runner._kanban_sub_op.assert_not_called()
    assert n.runner._kanban_rewind.call_count == 30
    assert "delivery held" in caplog.text
    adapter.send.return_value = SendResult(success=True)
    assert asyncio.run(n._send_pings()) is True
    n.runner._kanban_sub_op.assert_called_once()
