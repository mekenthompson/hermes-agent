"""The ``kanban_task_link`` hook names a tracked task as linked text in chat pings.

A plugin that tracks a Kanban task in an external system (an issue tracker) answers
``kanban_task_link`` with the task's ``{"url", "text"}``; the completed and review pings
then carry the title as a standard Markdown link that each adapter's own formatter
renders for its platform. A task no plugin answers for keeps today's ping byte for byte.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli.plugins import VALID_HOOKS, get_plugin_manager

URL = "https://tracker.example/team/issue/HF-123/fix-the-thing"


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)


async def _one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _ping(monkeypatch, *, review=False, title="HF-123: Fix the thing"):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title=title, assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        if review:
            kb.request_review(conn, tid, summary="ready")
        else:
            kb.complete_task(conn, tid, summary="shipped")
    finally:
        conn.close()
    adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    asyncio.run(_one_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    return tid, adapter.sent[0]


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()


@pytest.fixture
def link_hook():
    """Install a ``kanban_task_link`` answer in the registry ``invoke_hook`` reads."""
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    calls = []

    def install(answer):
        def callback(**kwargs):
            calls.append(kwargs)
            return answer(kwargs) if callable(answer) else answer
        mgr._hooks.setdefault("kanban_task_link", []).append(callback)
    install.calls = calls
    try:
        yield install
    finally:
        mgr._hooks = saved


def test_kanban_task_link_is_a_registered_hook():
    assert "kanban_task_link" in VALID_HOOKS


@pytest.mark.parametrize("review", [False, True])
def test_tracked_task_ping_names_the_task_as_a_link(board, monkeypatch, link_hook, review):
    link_hook(lambda kw: {"url": URL, "text": "HF-123 Fix the thing"})
    tid, text = _ping(monkeypatch, review=review)
    assert f"[HF-123 Fix the thing]({URL})" in text
    assert "HF-123: Fix the thing" not in text
    call = link_hook.calls[0]
    assert call["task_id"] == tid and call["title"] == "HF-123: Fix the thing"
    assert call["board"] == "default"


def test_untracked_task_ping_is_unchanged(board, monkeypatch, link_hook):
    first, baseline = _ping(monkeypatch)
    link_hook(None)  # a plugin is loaded but does not track this task
    second, text = _ping(monkeypatch)
    assert link_hook.calls and link_hook.calls[0]["task_id"] == second
    assert text.replace(second, "<task>") == baseline.replace(first, "<task>")
    assert "](" not in text


@pytest.mark.parametrize("answer", [
    {"url": "javascript:alert(1)", "text": "HF-123"},
    {"url": "https://tracker.example/a b", "text": "HF-123"},
    {"url": "https://tracker.example/a)b", "text": "HF-123"},
    {"url": URL, "text": "   "},
    {"url": URL},
    "https://tracker.example/plain-string",
])
def test_malformed_link_answer_keeps_the_plain_title(board, monkeypatch, link_hook, answer):
    link_hook(answer)
    _, text = _ping(monkeypatch)
    assert "HF-123: Fix the thing" in text
    assert "](" not in text


def test_link_text_brackets_cannot_break_the_markdown(board, monkeypatch, link_hook):
    link_hook({"url": URL, "text": "HF-123 [draft]\nsecond line"})
    _, text = _ping(monkeypatch)
    assert f"[HF-123 (draft) second line]({URL})" in text


def test_failing_link_plugin_keeps_the_plain_title(board, monkeypatch, link_hook):
    def boom(_kw):
        raise RuntimeError("tracker store unavailable")
    link_hook(boom)
    _, text = _ping(monkeypatch)
    assert "HF-123: Fix the thing" in text
