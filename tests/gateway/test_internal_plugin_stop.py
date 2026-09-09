"""Exact-execution stop contract for profile-local plugin turns."""

import asyncio
import threading
from collections import OrderedDict
from types import SimpleNamespace

from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway import execution_lifecycle
from gateway import run as gateway_run
from gateway.run import GatewayRunner
from gateway.session import SessionSource


_EXECUTION_ID = "plugin-exec-001"


def _event() -> MessageEvent:
    return MessageEvent(
        text="run the already-approved job",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.LOCAL,
            chat_id="linear-session-1",
            chat_type="dm",
            user_id="linear-agent",
            user_name="Linear Agent",
            scope_id="workspace-1",
            profile="aggie",
        ),
        internal=True,
        allow_gateway_control=False,
        metadata={},
    )


@pytest.mark.asyncio
async def test_dispatch_accepts_canonical_execution_id_without_changing_normal_result():
    runner = object.__new__(GatewayRunner)
    handler = AsyncMock(return_value="agent response")
    runner._primary_message_handler = lambda: handler
    event = _event()

    result = await runner.dispatch_internal_plugin_event(
        event,
        execution_id=_EXECUTION_ID,
    )

    assert result == "agent response"
    handler.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_dispatch_rejects_noncanonical_execution_id_before_handler_runs():
    runner = object.__new__(GatewayRunner)
    handler = AsyncMock(return_value="must not run")
    runner._primary_message_handler = lambda: handler

    with pytest.raises(ValueError, match="execution_id"):
        await runner.dispatch_internal_plugin_event(_event(), execution_id="bad id")

    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_interrupts_only_exact_bound_agent_without_releasing_turn_state():
    runner = object.__new__(GatewayRunner)
    session_key = "local:aggie:linear-session-1"
    agent = object()
    event = _event()
    event._internal_plugin_execution_id = _EXECUTION_ID
    state = runner._session_state(session_key)
    state.persistent.run_generation = 6
    runner._register_internal_plugin_execution(event, session_key)
    state.persistent.run_generation = 7
    state.turn.agent = agent
    runner._bind_internal_plugin_execution(
        event, session_key=session_key, run_generation=7, agent=agent
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        seen = []
        monkeypatch.setattr(
            "gateway.run.request_hard_interrupt",
            lambda *args: (seen.append(args), True)[1],
        )
        receipt = await runner.request_stop(
            session_key=session_key,
            expected_execution_id=_EXECUTION_ID,
            reason="plugin requested stop",
        )

    assert receipt == {
        "status": "accepted",
        "session_key": session_key,
        "execution_id": _EXECUTION_ID,
    }
    assert seen == [(agent, "plugin requested stop")]
    assert state.turn.agent is agent
    assert state.persistent.run_generation == 7


@pytest.mark.asyncio
async def test_promoted_execution_stop_acknowledges_interrupt_without_claiming_worker_stopped():
    """A Stop delivery is cooperative: a live worker may still be draining."""
    runner = object.__new__(GatewayRunner)
    session_key = "local:aggie:linear-session-1"
    event = _event()
    event._internal_plugin_execution_id = _EXECUTION_ID
    state = runner._session_state(session_key)
    state.persistent.run_generation = 6
    runner._register_internal_plugin_execution(event, session_key)
    state.persistent.run_generation = 7

    worker_waiting = threading.Event()
    interrupt_acknowledged = threading.Event()
    release_worker = threading.Event()

    class BlockingAgent:
        def hard_interrupt(self, reason):
            assert reason == "plugin requested stop"
            interrupt_acknowledged.set()

    agent = BlockingAgent()

    def worker():
        worker_waiting.set()
        assert release_worker.wait(timeout=2)

    thread = threading.Thread(target=worker)
    thread.start()
    assert worker_waiting.wait(timeout=1)

    try:
        assert runner._promote_running_agent(
            session_key=session_key,
            run_generation=7,
            agent=agent,
            internal_plugin_execution_id=_EXECUTION_ID,
        )

        receipt = await runner.request_stop(
            session_key=session_key,
            expected_execution_id=_EXECUTION_ID,
            reason="plugin requested stop",
        )

        assert receipt == {
            "status": "accepted",
            "session_key": session_key,
            "execution_id": _EXECUTION_ID,
        }
        assert interrupt_acknowledged.wait(timeout=1)
        assert thread.is_alive()
        assert state.turn.agent is agent
        assert state.persistent.run_generation == 7
    finally:
        release_worker.set()
        thread.join(timeout=2)

    assert not thread.is_alive()


def _register_bound_execution(runner, *, session_key, agent):
    event = _event()
    event._internal_plugin_execution_id = _EXECUTION_ID
    state = runner._session_state(session_key)
    state.persistent.run_generation = 6
    runner._register_internal_plugin_execution(event, session_key)
    state.persistent.run_generation = 7
    state.turn.agent = agent
    assert runner._bind_internal_plugin_execution(
        event, session_key=session_key, run_generation=7, agent=agent
    )
    return event, state


@pytest.mark.asyncio
async def test_stop_before_live_agent_promotion_is_accepted_and_fences_launch(tmp_path, monkeypatch):
    """Preparation has a receipt target even though it has no interruptable agent yet."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = object.__new__(GatewayRunner)
    session_key = "local:aggie:linear-session-1"
    event = _event()
    event._internal_plugin_execution_id = _EXECUTION_ID
    state = runner._session_state(session_key)
    state.persistent.run_generation = 6
    runner._register_internal_plugin_execution(event, session_key)
    state.persistent.run_generation = 7
    state.turn.agent = gateway_run._AGENT_PENDING_SENTINEL

    receipt = await runner.request_stop(
        session_key=session_key,
        expected_execution_id=_EXECUTION_ID,
    )

    assert receipt["status"] == "accepted"
    assert runner._internal_plugin_execution_records()[_EXECUTION_ID]["agent"] is None
    assert not runner._promote_running_agent(
        session_key=session_key, run_generation=7, agent=object(),
        internal_plugin_execution_id=_EXECUTION_ID,
    )


@pytest.mark.asyncio
async def test_stop_without_supported_interrupt_is_not_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = object.__new__(GatewayRunner)
    session_key = "local:aggie:linear-session-1"
    agent = object()
    _register_bound_execution(runner, session_key=session_key, agent=agent)

    receipt = await runner.request_stop(
        session_key=session_key,
        expected_execution_id=_EXECUTION_ID,
    )

    assert receipt["status"] != "accepted"
    assert runner._internal_plugin_execution_records()[_EXECUTION_ID]["agent"] is agent


@pytest.mark.asyncio
async def test_stop_for_wrong_session_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = object.__new__(GatewayRunner)
    session_key = "local:aggie:linear-session-1"
    _register_bound_execution(runner, session_key=session_key, agent=object())

    receipt = await runner.request_stop(
        session_key="local:aggie:other-session",
        expected_execution_id=_EXECUTION_ID,
    )

    assert receipt["status"] == "stale"


@pytest.mark.asyncio
async def test_stop_after_bound_generation_changes_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = object.__new__(GatewayRunner)
    session_key = "local:aggie:linear-session-1"
    _event, state = _register_bound_execution(
        runner, session_key=session_key, agent=object()
    )
    state.persistent.run_generation = 8

    receipt = await runner.request_stop(
        session_key=session_key,
        expected_execution_id=_EXECUTION_ID,
    )

    assert receipt["status"] == "stale"


@pytest.mark.asyncio
async def test_real_internal_plugin_dispatch_retires_normal_handler_execution(
    tmp_path, monkeypatch
):
    """Dispatch owns retirement only after its real handler returns normally."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = object.__new__(GatewayRunner)
    # Main's canonical route includes agent/chat-kind; derive it instead of
    # duplicating the retired pre-reconcile key format.
    session_key = runner._session_key_for_source(_event().source)

    async def handler(event):
        state = runner._session_state(session_key)
        state.persistent.run_generation = 1
        assert runner._promote_running_agent(
            session_key=session_key,
            run_generation=1,
            agent=object(),
            internal_plugin_execution_id=event._internal_plugin_execution_id,
        )
        return "stub executor result"

    runner._primary_message_handler = lambda: handler

    assert await runner.dispatch_internal_plugin_event(
        _event(), execution_id=_EXECUTION_ID
    ) == "stub executor result"
    assert _EXECUTION_ID not in runner._internal_plugin_execution_records()
    assert _EXECUTION_ID in runner._internal_plugin_retired_executions


@pytest.mark.asyncio
async def test_wrapper_cancellation_retains_execution_record(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = object.__new__(GatewayRunner)
    started = threading.Event()
    release = asyncio.Event()

    async def handler(_event):
        started.set()
        await release.wait()

    runner._primary_message_handler = lambda: handler
    task = asyncio.create_task(
        runner.dispatch_internal_plugin_event(_event(), execution_id=_EXECUTION_ID)
    )
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _EXECUTION_ID in runner._internal_plugin_execution_records()


def _retire_tool_using_execution(runner, session_key, *, worker_done=None):
    """Register, run a tool, then retire an execution with unknown occupancy."""
    event = _event()
    event._internal_plugin_execution_id = _EXECUTION_ID
    state = runner._session_state(session_key)
    state.persistent.run_generation = 6
    runner._register_internal_plugin_execution(event, session_key)
    runner._observe_internal_plugin_tool_event(_EXECUTION_ID, "tool.started")
    runner._observe_internal_plugin_tool_event(_EXECUTION_ID, "tool.completed")
    if worker_done is not None:
        runner._track_internal_plugin_execution_worker(_EXECUTION_ID, worker_done)
    runner._complete_internal_plugin_execution(_EXECUTION_ID, wrapper_completed=True)
    return event


@pytest.mark.asyncio
async def test_unknown_effect_tombstone_quarantines_the_session_until_the_quarantine_expires():
    """A returned worker is not permission to overlap an unproved prior effect.

    The quarantine stays fail-closed while the execution is unproven, but it is not
    permanent: with no worker Event to observe it expires on the TTL instead of rejecting
    every later execution on this session for the process lifetime.
    """
    runner = object.__new__(GatewayRunner)
    session_key = "local:aggie:linear-session-1"
    _retire_tool_using_execution(runner, session_key)
    record_tombstone = runner._internal_plugin_retired_executions[_EXECUTION_ID]
    assert record_tombstone["occupancy"] == "unknown"

    assert (await runner.get_execution_lifecycle(
        session_key=session_key, execution_id=_EXECUTION_ID
    ))["occupancy"] == "unknown"

    replacement = _event()
    replacement._internal_plugin_execution_id = "plugin-exec-002"
    with pytest.raises(ValueError, match="quarantined"):
        runner._register_internal_plugin_execution(replacement, session_key)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(execution_lifecycle, "QUARANTINE_TTL_SECONDS", 0.0)
        runner._register_internal_plugin_execution(replacement, session_key)
    assert "plugin-exec-002" in runner._internal_plugin_execution_records()
    # The unknown receipt itself is retained: expiry reopens the session, it does not
    # rewrite history into "released".
    assert runner._internal_plugin_retired_executions[_EXECUTION_ID]["occupancy"] == "unknown"


@pytest.mark.asyncio
async def test_finished_worker_lifts_quarantine_for_the_next_execution():
    """A tool-using turn whose worker Event is set no longer blocks the next execution."""
    runner = object.__new__(GatewayRunner)
    session_key = "local:aggie:linear-session-1"
    worker_done = threading.Event()
    worker_done.set()
    _retire_tool_using_execution(runner, session_key, worker_done=worker_done)
    assert runner._internal_plugin_retired_executions[_EXECUTION_ID]["occupancy"] == "unknown"

    replacement = _event()
    replacement._internal_plugin_execution_id = "plugin-exec-002"
    runner._register_internal_plugin_execution(replacement, session_key)
    assert "plugin-exec-002" in runner._internal_plugin_execution_records()


@pytest.mark.asyncio
async def test_live_worker_keeps_quarantine_closed():
    """An unfinished worker Event is exactly the case the quarantine exists for."""
    runner = object.__new__(GatewayRunner)
    session_key = "local:aggie:linear-session-1"
    event = _event()
    event._internal_plugin_execution_id = _EXECUTION_ID
    state = runner._session_state(session_key)
    state.persistent.run_generation = 6
    runner._register_internal_plugin_execution(event, session_key)
    runner._observe_internal_plugin_tool_event(_EXECUTION_ID, "tool.started")
    runner._track_internal_plugin_execution_worker(_EXECUTION_ID, threading.Event())
    runner._retire_internal_plugin_execution(_EXECUTION_ID, state="failed", occupancy="unknown")

    replacement = _event()
    replacement._internal_plugin_execution_id = "plugin-exec-002"
    with pytest.raises(ValueError, match="quarantined"):
        runner._register_internal_plugin_execution(replacement, session_key)


def test_retired_tombstones_stay_capped_behind_an_unknown_head():
    """The cap must not stop evicting the moment the oldest receipt is unknown."""
    runner = object.__new__(GatewayRunner)
    retired = runner.__dict__.setdefault(
        "_internal_plugin_retired_executions", OrderedDict()
    )
    total = execution_lifecycle.RETIRED_TOMBSTONE_CAP * 4
    for index in range(total):
        # First entry (and every 100th) is an unknown receipt, so the head is unknown.
        occupancy = "unknown" if index % 100 == 0 else "released"
        retired[f"exec-{index:05d}"] = {
            "session_key": "local:aggie:linear-session-1",
            "execution_id": f"exec-{index:05d}",
            "generation": 1, "state": "completed", "occupancy": occupancy,
            "tools": "none", "children": "none", "processes": "none", "remote": "none",
        }
        runner._prune_retired_executions(retired)

    assert len(retired) <= (
        execution_lifecycle.RETIRED_TOMBSTONE_CAP + execution_lifecycle.UNKNOWN_TOMBSTONE_CAP
    )
    assert len(retired) < total
    # Unknown receipts survive eviction of the ordinary ones.
    assert any(r["occupancy"] == "unknown" for r in retired.values())


def test_sweep_retires_records_whose_worker_event_is_set():
    """Nothing else reclaims a record left behind by a dispatch that raised."""
    runner = object.__new__(GatewayRunner)
    session_key = "local:aggie:linear-session-1"
    event = _event()
    event._internal_plugin_execution_id = _EXECUTION_ID
    runner._register_internal_plugin_execution(event, session_key)
    live = threading.Event()
    runner._track_internal_plugin_execution_worker(_EXECUTION_ID, live)

    assert runner._sweep_internal_plugin_executions() == 0
    assert _EXECUTION_ID in runner._internal_plugin_execution_records()

    live.set()
    assert runner._sweep_internal_plugin_executions() == 1
    assert _EXECUTION_ID not in runner._internal_plugin_execution_records()


@pytest.mark.asyncio
async def test_completed_no_tool_execution_allows_same_session_replacement():
    """The quarantine is conservative without making ordinary no-tool turns unusable."""
    runner = object.__new__(GatewayRunner)
    session_key = "local:aggie:linear-session-1"
    event = _event()
    event._internal_plugin_execution_id = _EXECUTION_ID
    runner._register_internal_plugin_execution(event, session_key)
    runner._complete_internal_plugin_execution(_EXECUTION_ID, wrapper_completed=True)

    replacement = _event()
    replacement._internal_plugin_execution_id = "plugin-exec-002"
    runner._register_internal_plugin_execution(replacement, session_key)
    assert "plugin-exec-002" in runner._internal_plugin_execution_records()


@pytest.mark.asyncio
async def test_stop_during_registration_before_session_claim_is_accepted_and_fences_launch():
    """Registration creates the stop target before the normal turn claim exists."""
    runner = object.__new__(GatewayRunner)
    session_key = "local:aggie:linear-session-1"
    event = _event()
    event._internal_plugin_execution_id = _EXECUTION_ID

    runner._register_internal_plugin_execution(event, session_key)
    assert runner._peek_session_state(session_key) is not None
    assert (await runner.request_stop(
        session_key=session_key, expected_execution_id=_EXECUTION_ID
    ))["status"] == "accepted"

    state = runner._session_state(session_key)
    state.persistent.run_generation = 1
    assert not runner._promote_running_agent(
        session_key=session_key, run_generation=1, agent=object(),
        internal_plugin_execution_id=_EXECUTION_ID,
    )


@pytest.mark.asyncio
async def test_real_to_thread_cancellation_does_not_release_until_physical_worker_finishes():
    """An asyncio cancellation is not evidence that the executor thread has stopped."""
    runner = object.__new__(GatewayRunner)
    session_key = "local:aggie:linear-session-1"
    event = _event()
    event._internal_plugin_execution_id = _EXECUTION_ID
    state = runner._session_state(session_key)
    state.persistent.run_generation = 6
    runner._register_internal_plugin_execution(event, session_key)
    state.persistent.run_generation = 7
    assert runner._promote_running_agent(
        session_key=session_key, run_generation=7, agent=object(),
        internal_plugin_execution_id=_EXECUTION_ID,
    )

    started, release, worker_done = threading.Event(), threading.Event(), threading.Event()

    def blocking_worker():
        started.set()
        try:
            assert release.wait(timeout=2)
        finally:
            worker_done.set()

    wrapper = asyncio.create_task(asyncio.to_thread(blocking_worker))
    assert await asyncio.to_thread(started.wait, 1)
    runner._track_internal_plugin_execution_worker(_EXECUTION_ID, worker_done)
    wrapper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wrapper

    live = await runner.get_execution_lifecycle(session_key=session_key, execution_id=_EXECUTION_ID)
    assert live["occupancy"] == "occupied"
    assert live["state"] == "running"

    release.set()
    assert await asyncio.to_thread(worker_done.wait, 1)
    retired = await runner.get_execution_lifecycle(session_key=session_key, execution_id=_EXECUTION_ID)
    assert retired == {
        "lifecycle_version": "execution-lifecycle/v2", "session_key": session_key,
        "execution_id": _EXECUTION_ID, "generation": 7, "state": "completed",
        "occupancy": "released", "tools": "none", "children": "none",
        "processes": "none", "remote": "none",
    }


@pytest.mark.asyncio
async def test_stale_internal_plugin_promotion_releases_launch_fence():
    """A stale generation must not leave the executor blocked at the launch fence."""
    runner = object.__new__(GatewayRunner)
    runner._is_session_run_current = lambda *_args: False
    gate = threading.Event()
    turn_ctx = SimpleNamespace(
        session_key="local:aggie:linear-session-1",
        run_generation=7,
        agent_holder=[object()],
        internal_plugin_execution_id=_EXECUTION_ID,
        execution_launch_gate=gate,
        execution_launch_allowed=False,
    )

    await runner._run_agent_track_agent(turn_ctx)

    assert turn_ctx.execution_launch_allowed is False
    assert gate.is_set()


@pytest.mark.asyncio
async def test_cancelled_agent_tracker_releases_launch_fence():
    """The tracker task is cancelled unconditionally during turn cleanup.

    A cancellation between spawn and promotion used to strand the executor thread parked at
    the launch fence for the process lifetime, so shutdown could never quiesce it.
    """
    runner = object.__new__(GatewayRunner)
    gate = threading.Event()
    turn_ctx = SimpleNamespace(
        session_key="local:aggie:linear-session-1",
        run_generation=7,
        agent_holder=[None],  # never promoted: the tracker is still polling
        internal_plugin_execution_id=_EXECUTION_ID,
        execution_launch_gate=gate,
        execution_launch_allowed=False,
    )

    task = asyncio.create_task(runner._run_agent_track_agent(turn_ctx))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gate.is_set()
    assert turn_ctx.execution_launch_allowed is False


@pytest.mark.asyncio
async def test_tracker_without_session_key_releases_launch_fence():
    """The early `not session_key` return is also a fence release."""
    runner = object.__new__(GatewayRunner)
    gate = threading.Event()
    turn_ctx = SimpleNamespace(
        session_key="", run_generation=7, agent_holder=[object()],
        internal_plugin_execution_id=_EXECUTION_ID,
        execution_launch_gate=gate, execution_launch_allowed=False,
    )

    await runner._run_agent_track_agent(turn_ctx)

    assert gate.is_set()
    assert turn_ctx.execution_launch_allowed is False


def test_launch_fence_wait_is_bounded_and_fails_closed(monkeypatch):
    """A gate nobody sets denies launch instead of parking the executor thread forever."""
    from gateway import run_turn_runner

    monkeypatch.setattr(run_turn_runner, "EXECUTION_LAUNCH_GATE_TIMEOUT", 0.05)
    ctx = SimpleNamespace(
        session_key="local:aggie:linear-session-1",
        execution_launch_gate=threading.Event(),
        execution_launch_allowed=True,  # stale optimism must not survive the timeout
    )
    fence_runner = object.__new__(run_turn_runner.TurnRunner)
    fence_runner._ctx = ctx

    assert fence_runner._await_execution_launch(_EXECUTION_ID) is False
    assert ctx.execution_launch_allowed is False

    ctx.execution_launch_gate.set()
    ctx.execution_launch_allowed = True
    assert fence_runner._await_execution_launch(_EXECUTION_ID) is True
