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
    collect_active_work,
    reconcile_active_work_result,
    ScanResult,
)
from hermes_cli.heartbeat import (
    ACTIVE_WORK_HEARTBEAT_ORIGIN,
    ACTIVE_WORK_LIVENESS_FLOOR_SECONDS,
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


def test_failed_scan_does_not_clear_an_existing_check(heartbeat_db, monkeypatch):
    reconcile_active_work_result(["work-session"])
    monkeypatch.setattr(
        "hermes_cli.active_work_progress._scan_delegation_sessions",
        lambda: ScanResult(set(), False),
    )
    ids, complete = collect_active_session_ids(
        background_ids=set(),
    )
    assert complete is False
    settled = reconcile_active_work_result(ids, scans_complete=complete)
    assert settled.cleared == []
    kept = load_heartbeat("work-session")
    assert kept is not None
    assert kept.origin == ACTIVE_WORK_HEARTBEAT_ORIGIN


def test_restart_reloads_managed_check_and_does_not_refire_settled_work(heartbeat_db):
    reconcile_active_work_result(["work-session"])
    reloaded = HeartbeatManager("work-session")
    reloaded.reload()
    assert reloaded.state.origin == ACTIVE_WORK_HEARTBEAT_ORIGIN
    assert reloaded.state.interval_seconds == 180
    reconcile_active_work_result([])
    assert HeartbeatManager("work-session").reload() is None


def test_collect_skips_child_handoff_and_stopped_delegations():
    ids, complete = collect_active_session_ids(
        delegation_ids={"delegate-parent"}, background_ids={"job-parent"},
    )
    assert ids == {"delegate-parent", "job-parent"}
    assert complete is True

    class _Proc:
        def __init__(self, parent, handoff=""):
            self.parent_session_id = parent
            self.handoff_note = handoff

    registry = SimpleNamespace(_running={
        "build": _Proc("human-session"),
        "child-build": _Proc("child-session", handoff="subagent handed off npm ci"),
    })
    from hermes_cli import active_work_progress as progress
    assert progress._scan_background_sessions(registry).ids == {"human-session"}


def test_native_kanban_does_not_arm_a_second_progress_heartbeat():
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    conn = connect()
    try:
        task_id = kb.create_task(conn, title="Native work", session_id="native-origin",
                                 workspace_kind="scratch")
        assert kb.claim_task(conn, task_id, claimer="native-worker") is not None
        assert kb.get_task(conn, task_id).status == "running"
        ids, complete = collect_active_session_ids(delegation_ids=set(), background_ids=set())
        assert ids == set()
        assert complete is True
    finally:
        conn.close()


def test_delegation_scan_ignores_stopped_rows(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE async_delegations (origin_session_id TEXT, parent_session_id TEXT, "
        "delegation_id TEXT, state TEXT, stop_state TEXT)"
    )
    conn.execute(
        "INSERT INTO async_delegations VALUES ('live-parent', '', 'd-live-parent', 'running', '')"
    )
    conn.execute(
        "INSERT INTO async_delegations VALUES ('stalling-parent', '', 'd-stalling-parent', 'stalling', '')"
    )
    conn.execute(
        "INSERT INTO async_delegations VALUES ('finalizing-parent', '', 'd-finalizing-parent', 'finalizing', '')"
    )
    conn.execute(
        "INSERT INTO async_delegations VALUES ('stopped-parent', '', 'd-stopped-parent', 'running', 'requested')"
    )
    conn.execute(
        "INSERT INTO async_delegations VALUES ('stopped-stalling', '', 'd-stopped-stalling', 'stalling', 'requested')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("hermes_cli.active_work_progress._state_db_path", lambda: db)
    from hermes_cli.active_work_progress import _scan_delegation_sessions
    assert _scan_delegation_sessions().ids == {
        "live-parent", "stalling-parent", "finalizing-parent",
    }


class _Adapter(BasePlatformAdapter):
    def __init__(self, config, platform):
        super().__init__(config, platform)
        self.last_send: dict = {}
    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.last_send = {"chat_id": chat_id, "content": content, "metadata": metadata or {}}
        return SendResult(success=True, message_id="wire-1")


def _managed_check(session_id="origin-session", fingerprint="fp-1"):
    """What one reconcile scan does for a session whose tracked work currently fingerprints as given."""
    return HeartbeatManager(session_id).ensure_managed(
        ACTIVE_WORK_PROMPT, ACTIVE_WORK_INTERVAL_SECONDS, origin=ACTIVE_WORK_HEARTBEAT_ORIGIN,
        work_fingerprint=fingerprint,
    )


def _poll_harness(monkeypatch):
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
    return SimpleNamespace(
        clock=clock, adapter=adapter, received=received, runner=runner,
        watch={key: (source, "origin-session")},
    )


@pytest.mark.asyncio
async def test_due_check_delivers_to_originating_chat_and_silence_is_not_a_send(heartbeat_db, monkeypatch):
    h = _poll_harness(monkeypatch)
    adapter, received = h.adapter, h.received
    _managed_check(fingerprint="fp-1")
    _managed_check(fingerprint="fp-2")  # a delegation moved on since the check was installed
    h.clock.now += ACTIVE_WORK_INTERVAL_SECONDS
    await h.runner._heartbeat_poll_once(h.watch)
    await asyncio.gather(*adapter._background_tasks)
    assert len(received) == 1
    event = received[0]
    assert event.source.chat_id == "D-origin"
    assert event.source.thread_id == "1791695152.254539"
    assert event.source.platform == Platform.SLACK
    assert ACTIVE_WORK_PROMPT in event.text
    assert "[SILENT]" in event.text
    assert "nothing has changed" not in event.text
    assert "child-process" in event.text
    from gateway.platforms.base import _thread_metadata_for_source
    progress = "Build still running, next is the canary."
    assert is_intentional_silence_response("[SILENT]") is True
    assert is_intentional_silence_response(progress) is False
    sent = await adapter.send(
        event.source.chat_id, progress, metadata=_thread_metadata_for_source(event.source),
    )
    assert sent.success is True
    assert adapter.last_send["chat_id"] == "D-origin"
    assert adapter.last_send["metadata"]["thread_id"] == "1791695152.254539"


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
        "hermes_cli.active_work_progress.collect_active_work",
        lambda **_kwargs: ({"origin-session": "fp-1"}, True),
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
        "hermes_cli.active_work_progress.collect_active_work",
        lambda **_kwargs: ({}, True),
    )
    await reconcile_active_work_watches(runner)
    assert key not in runner._heartbeat_watch
    assert load_heartbeat("origin-session") is None


def _watch_runner(entries):
    runner = SimpleNamespace(
        session_store=SimpleNamespace(
            list_sessions=lambda: entries,
            _routing_home=None,
            peek_session_id=lambda session_key: next(
                (entry.session_id for entry in entries if entry.session_key == session_key),
                None,
            ),
        ),
        _heartbeat_watch={},
        _run_in_executor_with_context=asyncio.to_thread,
        config=SimpleNamespace(multiplex_profiles=False),
    )
    runner._register_heartbeat_watch = lambda quick_key, src, sid: runner._heartbeat_watch.__setitem__(
        quick_key, (src, sid)
    )
    runner._unregister_heartbeat_watch = lambda quick_key: runner._heartbeat_watch.pop(quick_key, None)
    runner._start_heartbeat_poller = lambda: None
    runner._restored_source = lambda entry: entry.origin
    runner._profile_scope_key_for_source = lambda _source: "default"

    from contextlib import contextmanager

    @contextmanager
    def _scope(_source):
        yield

    runner._profile_scope_for_source = _scope
    return runner


@pytest.mark.asyncio
async def test_reconcile_keeps_watch_on_compression_continuation(heartbeat_db, monkeypatch):
    source = SessionSource(platform=Platform.SLACK, chat_id="D9", chat_type="dm", thread_id="t1")
    key = build_session_key(source)
    entry = SimpleNamespace(
        session_key=key, session_id="child-session", prev_session_id="parent-session",
        origin=source, suspended=False,
    )
    runner = _watch_runner([entry])
    runner.session_store._compression_tip_for_session_id = (
        lambda sid: "child-session" if sid == "parent-session" else sid
    )
    HeartbeatManager("child-session").ensure_managed(
        ACTIVE_WORK_PROMPT, ACTIVE_WORK_INTERVAL_SECONDS, origin=ACTIVE_WORK_HEARTBEAT_ORIGIN,
    )
    monkeypatch.setattr(
        "hermes_cli.active_work_progress.collect_active_work",
        lambda **_kwargs: ({"parent-session": "fp-1"}, True),
    )
    monkeypatch.setattr(
        "gateway.run_heartbeat_restore._watched_homes",
        lambda _runner, home: [home],
    )
    await reconcile_active_work_watches(runner)
    assert runner._heartbeat_watch[key][1] == "child-session"
    assert load_heartbeat("child-session").origin == ACTIVE_WORK_HEARTBEAT_ORIGIN
    assert load_heartbeat("parent-session") is None


@pytest.mark.asyncio
async def test_reconcile_then_restore_skips_non_deliverable_origin(heartbeat_db, monkeypatch):
    from gateway.run_heartbeat_restore import restore_heartbeat_watches

    source = SessionSource(platform=Platform.API_SERVER, chat_id="api-1", chat_type="dm")
    key = build_session_key(source)
    entry = SimpleNamespace(
        session_key=key, session_id="api-session", origin=source, suspended=False,
    )
    runner = _watch_runner([entry])
    HeartbeatManager("api-session").ensure_managed(
        ACTIVE_WORK_PROMPT, ACTIVE_WORK_INTERVAL_SECONDS, origin=ACTIVE_WORK_HEARTBEAT_ORIGIN,
    )
    monkeypatch.setattr(
        "hermes_cli.active_work_progress.collect_active_work",
        lambda **_kwargs: ({"api-session": "fp-1"}, True),
    )
    monkeypatch.setattr(
        "gateway.run_heartbeat_restore._watched_homes",
        lambda _runner, home: [home],
    )
    monkeypatch.setattr(
        "gateway.run_idle_gates.profile_has_active_heartbeat",
        lambda _home: True,
    )
    await reconcile_active_work_watches(runner)
    await restore_heartbeat_watches(runner)
    assert key not in runner._heartbeat_watch
    assert load_heartbeat("api-session") is None


def test_rendered_due_prompt_requires_exact_silence(heartbeat_db):
    HeartbeatManager("origin-session").ensure_managed(
        ACTIVE_WORK_PROMPT, ACTIVE_WORK_INTERVAL_SECONDS, origin=ACTIVE_WORK_HEARTBEAT_ORIGIN,
    )
    state = load_heartbeat("origin-session")
    state.created_at = 0
    state.last_fired_at = 0
    from hermes_cli.heartbeat import save_heartbeat
    save_heartbeat("origin-session", state)
    prompt = HeartbeatManager("origin-session").due_prompt(now=10**12)
    assert prompt is not None
    assert "[SILENT]" in prompt
    assert "nothing has changed" not in prompt


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
    import inspect
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS, _WAKE_KINDS

    assert "completed" in _WAKE_KINDS
    assert "blocked" in _WAKE_KINDS
    assert ACTIVE_WORK_INTERVAL_SECONDS == 180
    signature = inspect.signature(GatewayKanbanWatchersMixin._kanban_notifier_watcher)
    assert signature.parameters["interval"].default == 5.0
    notifier_source = inspect.getsource(__import__("gateway.kanban_watchers_notifier", fromlist=["*"]))
    assert "active_work_progress" not in notifier_source
    assert "ACTIVE_WORK_INTERVAL_SECONDS" not in notifier_source
    note = SimpleNamespace(head="Kanban", title="canary", ping_title="canary", task=SimpleNamespace(result="shipped"))
    completed_fmt = _EVENT_FORMATTERS["completed"]
    blocked_fmt = _EVENT_FORMATTERS["blocked"]
    completed, handoff, _review = completed_fmt(
        SimpleNamespace(kind="completed", payload={"summary": "shipped the canary"}), note,
    )
    blocked, _handoff, _review = blocked_fmt(
        SimpleNamespace(kind="blocked", payload={"reason": "needs a credential"}), note,
    )
    assert completed and "shipped" in completed
    assert handoff == "shipped the canary"
    assert blocked and "credential" in blocked
    assert "180" not in completed and "180" not in blocked


# --- event-driven wake: the 180s tick scans, only changed work (or the liveness floor) wakes the model ---

SID = "work-session"


def _tick(n):
    """The n-th 180s scan tick after the managed check was installed."""
    return load_heartbeat(SID).created_at + n * ACTIVE_WORK_INTERVAL_SECONDS


def test_unchanged_work_admits_no_prompt_at_any_due_tick(heartbeat_db):
    _managed_check(SID, "fp-1")
    for n in range(1, 8):  # 21 minutes of scans that all see the same work
        _managed_check(SID, "fp-1")
        assert HeartbeatManager(SID).due_prompt(now=_tick(n)) is None
    assert load_heartbeat(SID).fire_count == 0


def test_changed_work_wakes_once_then_waits_for_the_next_change(heartbeat_db):
    _managed_check(SID, "fp-1")
    _managed_check(SID, "fp-2")  # e.g. a delegation went running -> stalling
    prompt = HeartbeatManager(SID).due_prompt(now=_tick(1))
    assert prompt is not None and "[SILENT]" in prompt
    assert HeartbeatManager(SID).due_prompt(now=_tick(2)) is None  # same work as the last wake
    _managed_check(SID, "fp-3")
    assert HeartbeatManager(SID).due_prompt(now=_tick(3)) is not None
    assert load_heartbeat(SID).fire_count == 2


def test_a_change_inside_the_tick_interval_waits_for_the_tick(heartbeat_db):
    _managed_check(SID, "fp-1")
    _managed_check(SID, "fp-2")
    assert HeartbeatManager(SID).due_prompt(now=_tick(1) - 1) is None  # the 180s cadence still bounds wakes


def test_liveness_floor_wakes_once_for_work_that_never_changes(heartbeat_db):
    _managed_check(SID, "fp-1")
    created = load_heartbeat(SID).created_at
    assert HeartbeatManager(SID).due_prompt(now=created + ACTIVE_WORK_LIVENESS_FLOOR_SECONDS - 1) is None
    woken_at = created + ACTIVE_WORK_LIVENESS_FLOOR_SECONDS
    assert HeartbeatManager(SID).due_prompt(now=woken_at) is not None
    assert HeartbeatManager(SID).due_prompt(now=woken_at + ACTIVE_WORK_INTERVAL_SECONDS) is None
    assert HeartbeatManager(SID).due_prompt(now=woken_at + ACTIVE_WORK_LIVENESS_FLOOR_SECONDS) is not None


def test_fingerprints_survive_a_restart_and_a_woken_change_does_not_wake_twice(heartbeat_db):
    _managed_check(SID, "fp-1")
    _managed_check(SID, "fp-2")
    after_restart = HeartbeatManager(SID)  # a restarted gateway rebuilds the manager from the stored row
    assert (after_restart.state.work_fingerprint, after_restart.state.woken_fingerprint) == ("fp-2", "fp-1")
    assert after_restart.due_prompt(now=_tick(1)) is not None
    restarted_again = HeartbeatManager(SID)
    assert restarted_again.state.woken_fingerprint == "fp-2"
    assert restarted_again.due_prompt(now=_tick(2)) is None


def test_a_row_saved_before_fingerprints_existed_does_not_wake_for_the_upgrade(heartbeat_db):
    from hermes_cli.heartbeat import save_heartbeat

    save_heartbeat(SID, HeartbeatState(
        prompt=ACTIVE_WORK_PROMPT, interval_seconds=ACTIVE_WORK_INTERVAL_SECONDS,
        created_at=1_000.0, origin=ACTIVE_WORK_HEARTBEAT_ORIGIN,
    ))
    assert _managed_check(SID, "fp-1") == "kept"
    assert HeartbeatManager(SID).due_prompt(now=1_000.0 + ACTIVE_WORK_INTERVAL_SECONDS) is None


def test_an_abandoned_wake_keeps_the_change_pending(heartbeat_db):
    _managed_check(SID, "fp-1")
    _managed_check(SID, "fp-2")
    manager = HeartbeatManager(SID)
    assert manager.due_prompt(now=_tick(1)) is not None
    assert manager.abandon_fire() is True  # the turn never started
    assert HeartbeatManager(SID).due_prompt(now=_tick(1)) is not None


def test_user_heartbeat_fires_on_every_due_tick_and_is_never_fingerprinted(heartbeat_db):
    user = HeartbeatManager("user-session")
    user.set("my own check", 600)
    created = user.state.created_at
    assert user.due_prompt(now=created + 600) is not None
    assert user.due_prompt(now=created + 1200) is not None  # unchanged state, still fires: it is the user's
    assert _managed_check("user-session", "fp-9") == "preserved-user"
    stored = load_heartbeat("user-session")
    assert stored.origin == USER_HEARTBEAT_ORIGIN
    assert (stored.work_fingerprint, stored.woken_fingerprint) == ("", "")


@pytest.mark.asyncio
async def test_poll_admits_no_turn_while_work_is_unchanged_and_one_when_it_changes(heartbeat_db, monkeypatch):
    h = _poll_harness(monkeypatch)
    _managed_check(fingerprint="fp-1")
    for _ in range(3):
        h.clock.now += ACTIVE_WORK_INTERVAL_SECONDS
        await h.runner._heartbeat_poll_once(h.watch)
        await asyncio.gather(*h.adapter._background_tasks)
    assert h.received == []

    _managed_check(fingerprint="fp-2")
    h.clock.now += ACTIVE_WORK_INTERVAL_SECONDS
    await h.runner._heartbeat_poll_once(h.watch)
    await asyncio.gather(*h.adapter._background_tasks)
    assert len(h.received) == 1


def _delegation_db(path, rows):
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE async_delegations (origin_session_id TEXT, parent_session_id TEXT, "
        "delegation_id TEXT, state TEXT, stop_state TEXT)"
    )
    conn.executemany("INSERT INTO async_delegations VALUES (?, '', ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_work_fingerprint_follows_delegation_state_and_ignores_row_order(tmp_path, monkeypatch):
    rows = [("s", "d1", "running", ""), ("s", "d2", "running", ""), ("other", "d3", "running", "")]
    first, second = tmp_path / "first.db", tmp_path / "second.db"
    _delegation_db(first, rows)
    _delegation_db(second, list(reversed(rows)))
    stalled = tmp_path / "stalled.db"
    _delegation_db(stalled, [("s", "d1", "running", ""), ("s", "d2", "stalling", ""), ("other", "d3", "running", "")])
    monkeypatch.setattr("hermes_cli.active_work_progress._state_db_path", lambda: first)
    base, complete = collect_active_work(background_ids=set())
    monkeypatch.setattr("hermes_cli.active_work_progress._state_db_path", lambda: second)
    reordered, _ = collect_active_work(background_ids=set())
    monkeypatch.setattr("hermes_cli.active_work_progress._state_db_path", lambda: stalled)
    moved_on, _ = collect_active_work(background_ids=set())
    assert complete is True and set(base) == {"s", "other"}
    assert reordered == base
    assert moved_on["s"] != base["s"] and moved_on["other"] == base["other"]


def test_work_fingerprint_follows_background_process_membership_and_exit():
    from hermes_cli import active_work_progress as progress

    def proc(pid, exited=False):
        return SimpleNamespace(id=pid, parent_session_id="human-session", handoff_note="", exited=exited)

    one = progress._scan_background_sessions(SimpleNamespace(_running={"p1": proc("p1")}))
    two = progress._scan_background_sessions(SimpleNamespace(_running={"p1": proc("p1"), "p2": proc("p2")}))
    exited = progress._scan_background_sessions(SimpleNamespace(_running={"p1": proc("p1", exited=True)}))
    fingerprints = {name: progress.work_fingerprint(scan.units["human-session"])
                    for name, scan in (("one", one), ("two", two), ("exited", exited))}
    assert len(set(fingerprints.values())) == 3
