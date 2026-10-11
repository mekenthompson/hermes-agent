"""Active-work progress checks: preserve user heartbeats, deliver, then stop."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway.active_work_progress import _deliverable, reconcile_active_work_watches
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.response_filters import is_intentional_silence_response
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli.active_work_progress import (
    ACTIVE_WORK_INTERVAL_SECONDS,
    ACTIVE_WORK_PROMPT,
    collect_active_session_ids,
    reconcile_active_work_result,
)
from hermes_cli.heartbeat import (
    ACTIVE_WORK_HEARTBEAT_ORIGIN,
    USER_HEARTBEAT_ORIGIN,
    HeartbeatManager,
    HeartbeatState,
    load_heartbeat,
)


@pytest.fixture
def heartbeat_db(monkeypatch):
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield
    goals._DB_CACHE.clear()


def test_legacy_heartbeat_without_origin_is_user_authored():
    raw = json.dumps({
        "prompt": "watch the deploy", "interval_seconds": 600, "status": "active",
        "created_at": 1, "last_fired_at": 0, "fire_count": 0,
    })
    state = HeartbeatState.from_json(raw)
    assert state.origin == USER_HEARTBEAT_ORIGIN


def test_reconcile_preserves_user_heartbeat_and_clears_when_work_settles(heartbeat_db):
    HeartbeatManager("user-session").set("my own check", 600)
    first = reconcile_active_work_result(["user-session", "work-session"])
    assert first.actions["user-session"] == "preserved-user"
    assert first.actions["work-session"] == "installed"
    user = load_heartbeat("user-session")
    managed = load_heartbeat("work-session")
    assert user.origin == USER_HEARTBEAT_ORIGIN
    assert user.prompt == "my own check"
    assert managed.origin == ACTIVE_WORK_HEARTBEAT_ORIGIN
    assert managed.interval_seconds == ACTIVE_WORK_INTERVAL_SECONDS == 180
    assert "[SILENT]" in managed.prompt
    assert "child-process" in managed.prompt

    settled = reconcile_active_work_result([])
    assert "work-session" in settled.cleared
    assert load_heartbeat("work-session") is None
    assert load_heartbeat("user-session").prompt == "my own check"


def test_restart_reloads_managed_check_and_does_not_refire_settled_work(heartbeat_db):
    reconcile_active_work_result(["work-session"])
    reloaded = HeartbeatManager("work-session")
    reloaded.reload()
    assert reloaded.state.origin == ACTIVE_WORK_HEARTBEAT_ORIGIN
    assert reloaded.state.interval_seconds == 180
    reconcile_active_work_result([])
    assert HeartbeatManager("work-session").reload() is None


def test_collect_skips_child_handoff_and_stopped_delegations():
    assert collect_active_session_ids(
        kanban_ids={"kanban-parent"}, delegation_ids=set(), background_ids=set(),
    ) == {"kanban-parent"}

    class _Proc:
        def __init__(self, parent, handoff=""):
            self.parent_session_id = parent
            self.handoff_note = handoff

    registry = SimpleNamespace(_running={
        "build": _Proc("human-session"),
        "child-build": _Proc("child-session", handoff="subagent handed off npm ci"),
    })
    from hermes_cli import active_work_progress as progress
    assert progress._scan_background_sessions(registry) == {"human-session"}


def test_delegation_scan_ignores_stopped_rows(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE async_delegations (origin_session_id TEXT, parent_session_id TEXT, "
        "state TEXT, stop_state TEXT)"
    )
    conn.execute(
        "INSERT INTO async_delegations VALUES ('live-parent', '', 'running', '')"
    )
    conn.execute(
        "INSERT INTO async_delegations VALUES ('stopped-parent', '', 'running', 'requested')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("hermes_cli.active_work_progress._state_db_path", lambda: db)
    from hermes_cli.active_work_progress import _scan_delegation_sessions
    assert _scan_delegation_sessions() == {"live-parent"}


class _Adapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="wire-1")


@pytest.mark.asyncio
async def test_due_check_delivers_to_originating_chat_and_silence_is_not_a_send(heartbeat_db, monkeypatch):
    from hermes_cli import heartbeat as heartbeat_mod

    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(heartbeat_mod, "time", SimpleNamespace(time=lambda: clock.now))
    source = SessionSource(
        platform=Platform.SLACK, chat_id="D-origin", user_id="U1", chat_type="dm",
        thread_id="1791695152.254539",
    )
    key = build_session_key(source)
    adapter = _Adapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.SLACK)
    received = []

    async def handler(event):
        event._heartbeat_execution_started = True
        received.append(event)

    adapter.set_message_handler(handler)
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._delivery_adapter_for = lambda _source: adapter
    runner._run_in_executor_with_context = asyncio.to_thread
    HeartbeatManager("origin-session").ensure_managed(
        ACTIVE_WORK_PROMPT, ACTIVE_WORK_INTERVAL_SECONDS, origin=ACTIVE_WORK_HEARTBEAT_ORIGIN,
    )
    watch = {key: (source, "origin-session")}
    clock.now += ACTIVE_WORK_INTERVAL_SECONDS
    await runner._heartbeat_poll_once(watch)
    await asyncio.gather(*adapter._background_tasks)
    assert len(received) == 1
    event = received[0]
    assert event.source.chat_id == "D-origin"
    assert event.source.thread_id == "1791695152.254539"
    assert event.source.platform == Platform.SLACK
    assert "[SILENT]" in event.text
    assert "child-process" in event.text
    assert is_intentional_silence_response("[SILENT]") is True
    assert is_intentional_silence_response("Build still running, next is the canary.") is False


@pytest.mark.asyncio
async def test_reconcile_registers_origin_watch_and_drops_it_when_settled(heartbeat_db, monkeypatch):
    source = SessionSource(platform=Platform.SLACK, chat_id="D9", chat_type="dm", thread_id="t1")
    key = build_session_key(source)
    entry = SimpleNamespace(
        session_key=key, session_id="origin-session", origin=source, suspended=False,
    )
    runner = SimpleNamespace(
        session_store=SimpleNamespace(list_sessions=lambda: [entry], _routing_home=None),
        _heartbeat_watch={},
        _run_in_executor_with_context=asyncio.to_thread,
    )
    runner._register_heartbeat_watch = lambda quick_key, src, sid: runner._heartbeat_watch.__setitem__(
        quick_key, (src, sid)
    )
    runner._unregister_heartbeat_watch = lambda quick_key: runner._heartbeat_watch.pop(quick_key, None)
    runner._start_heartbeat_poller = lambda: None

    monkeypatch.setattr(
        "hermes_cli.active_work_progress.collect_active_session_ids",
        lambda **_kwargs: {"origin-session"},
    )
    monkeypatch.setattr(
        "gateway.run_heartbeat_restore._watched_homes",
        lambda _runner, home: [home],
    )
    await reconcile_active_work_watches(runner)
    assert key in runner._heartbeat_watch
    assert runner._heartbeat_watch[key][0].thread_id == "t1"
    assert load_heartbeat("origin-session").origin == ACTIVE_WORK_HEARTBEAT_ORIGIN

    monkeypatch.setattr(
        "hermes_cli.active_work_progress.collect_active_session_ids",
        lambda **_kwargs: set(),
    )
    await reconcile_active_work_watches(runner)
    assert key not in runner._heartbeat_watch
    assert load_heartbeat("origin-session") is None


def test_internal_platforms_are_not_progress_destinations():
    slack = SimpleNamespace(
        session_key="k", session_id="s", suspended=False,
        origin=SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="D1"),
    )
    a2a = SimpleNamespace(
        session_key="k2", session_id="s2", suspended=False,
        origin=SimpleNamespace(platform=SimpleNamespace(value="a2a"), chat_id="ctx-1"),
    )
    assert _deliverable(slack) is True
    assert _deliverable(a2a) is False


def test_notifier_terminal_kinds_are_not_gated_on_the_progress_interval():
    from gateway.kanban_watchers_notifier import _WAKE_KINDS
    assert "completed" in _WAKE_KINDS
    assert "blocked" in _WAKE_KINDS
    assert ACTIVE_WORK_INTERVAL_SECONDS == 180
