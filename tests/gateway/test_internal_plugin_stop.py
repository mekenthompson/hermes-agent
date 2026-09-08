"""Exact-execution stop contract for profile-local plugin turns."""

import asyncio
import json
import threading
from types import SimpleNamespace

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway import run as gateway_run
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
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
async def test_real_gateway_handler_executes_settled_builtin_tools_and_releases_receipt(
    tmp_path, monkeypatch,
):
    """A normal internal turn must prove real builtin calls before its receipt releases."""
    source_file = tmp_path / "real-tool-input.txt"
    source_file.write_text("real tool lifecycle evidence\n", encoding="utf-8")
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {
        "api_key": "offline-test", "base_url": "http://127.0.0.1:1/v1",
        "provider": "openai-compat",
    })
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *_args, **_kwargs: 100_000)

    def response(content="", finish_reason="stop", tool_calls=None):
        message = SimpleNamespace(content=content, tool_calls=tool_calls)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
            model="offline-test", usage=None,
        )

    calls = [
        SimpleNamespace(
            id="real-read-file", type="function",
            function=SimpleNamespace(name="read_file", arguments=json.dumps({"path": str(source_file)})),
        ),
        SimpleNamespace(
            id="real-search-files", type="function",
            function=SimpleNamespace(name="search_files", arguments=json.dumps({
                "path": str(tmp_path), "pattern": "lifecycle evidence",
            })),
        ),
    ]
    provider = MagicMock()
    provider.chat.completions.create.side_effect = [
        response(finish_reason="tool_calls", tool_calls=calls), response(content="tools completed"),
    ]
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", lambda **_kwargs: provider)

    runner = GatewayRunner(gateway_run.GatewayConfig())
    observed = []
    observe = runner._observe_internal_plugin_tool_event

    def record_lifecycle(*args, **kwargs):
        if args[1] in {"tool.started", "tool.completed"}:
            observed.append((args[1], kwargs["tool_call_id"], kwargs["tool_lifetime"]))
        return observe(*args, **kwargs)

    monkeypatch.setattr(runner, "_observe_internal_plugin_tool_event", record_lifecycle)
    event = _event()
    result = await runner.dispatch_internal_plugin_event(
        event, execution_id=_EXECUTION_ID,
        execution_policy={"max_iterations": 3, "wall_seconds": 45},
    )

    session_key = runner._session_key_for_source(event.source)
    receipt = await runner.get_execution_lifecycle(session_key=session_key, execution_id=_EXECUTION_ID)
    assert result == "tools completed"
    assert provider.chat.completions.create.call_count == 2
    assert observed[0:2] == [
        ("tool.started", "real-read-file", "settled"),
        ("tool.started", "real-search-files", "settled"),
    ]
    assert set(observed[2:]) == {
        ("tool.completed", "real-read-file", "settled"),
        ("tool.completed", "real-search-files", "settled"),
    }
    assert receipt["state"] == "completed"
    assert receipt["occupancy"] == "released"
    assert all(receipt[name] == "none" for name in ("tools", "children", "processes", "remote"))


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


@pytest.mark.asyncio
async def test_unknown_effect_tombstone_quarantines_the_session_from_replacement():
    """A returned worker is not permission to overlap an unproved prior effect."""
    runner = object.__new__(GatewayRunner)
    session_key = "local:aggie:linear-session-1"
    event = _event()
    event._internal_plugin_execution_id = _EXECUTION_ID
    state = runner._session_state(session_key)
    state.persistent.run_generation = 6
    runner._register_internal_plugin_execution(event, session_key)
    runner._observe_internal_plugin_tool_event(_EXECUTION_ID, "tool.started")
    runner._observe_internal_plugin_tool_event(_EXECUTION_ID, "tool.completed")
    record = runner._internal_plugin_execution_records()[_EXECUTION_ID]
    assert record["tool_lifetime_evidence_invalid"]
    runner._complete_internal_plugin_execution(_EXECUTION_ID, wrapper_completed=True)

    assert (await runner.get_execution_lifecycle(
        session_key=session_key, execution_id=_EXECUTION_ID
    ))["occupancy"] == "unknown"

    replacement = _event()
    replacement._internal_plugin_execution_id = "plugin-exec-002"
    with pytest.raises(ValueError, match="quarantined"):
        runner._register_internal_plugin_execution(replacement, session_key)


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


def _settled_tool_record(runner, *, lifetime="settled", completed=True):
    """Drive the same lifecycle events the normal gateway handler receives."""
    session_key = "local:aggie:linear-session-1"
    event = _event()
    event._internal_plugin_execution_id = _EXECUTION_ID
    state = runner._session_state(session_key)
    state.persistent.run_generation = 6
    runner._register_internal_plugin_execution(event, session_key)
    agent = SimpleNamespace(_active_children=[], _active_children_lock=threading.Lock())
    state.persistent.run_generation = 7
    assert runner._promote_running_agent(session_key=session_key, run_generation=7, agent=agent,
                                          internal_plugin_execution_id=_EXECUTION_ID)
    done = threading.Event()
    done.set()
    runner._track_internal_plugin_execution_worker(
        _EXECUTION_ID, done, task_id="lifetime-test-task", session_key=session_key,
        parent_session_id="lifetime-test-parent",
    )
    runner._observe_internal_plugin_tool_event(_EXECUTION_ID, "tool.started",
                                                tool_call_id="call-001", tool_lifetime=lifetime)
    if completed:
        runner._observe_internal_plugin_tool_event(_EXECUTION_ID, "tool.completed",
                                                    tool_call_id="call-001", tool_lifetime=lifetime)
    return session_key


@pytest.mark.asyncio
async def test_normal_handler_settled_builtin_call_releases_only_after_completed_event(monkeypatch):
    """The normal TurnRunner callback preserves IDs from a real tool event pair."""
    runner = object.__new__(GatewayRunner)
    session_key = _settled_tool_record(runner)
    normal_handler = object.__new__(TurnRunner)
    normal_handler._runner = runner
    normal_handler._ctx = SimpleNamespace(
        internal_plugin_execution_id=_EXECUTION_ID, log_queue=None, progress_queue=None,
        _live_status_adapter=None, _run_still_current=lambda: True,
    )
    # Replace the helper's direct events with the actual normal-handler event path.
    record = runner._internal_plugin_execution_records()[_EXECUTION_ID]
    record["tool_calls"] = {}
    normal_handler.progress_callback("tool.started", "read_file", "read", {},
                                     tool_call_id="call-001", tool_lifetime="settled")
    normal_handler.progress_callback("tool.completed", "read_file", None, None,
                                     tool_call_id="call-001", tool_lifetime="settled")
    monkeypatch.setattr("tools.process_registry.process_registry.has_active_processes", lambda _task: False)
    monkeypatch.setattr("tools.async_delegation.has_live_for_session", lambda **_selectors: False)
    receipt = await runner.get_execution_lifecycle(session_key=session_key, execution_id=_EXECUTION_ID)
    assert receipt["occupancy"] == "released"
    assert receipt["tools"] == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("lifetime", ["unknown", "may_spawn"])
async def test_unknown_or_detached_lifetime_remains_occupied(monkeypatch, lifetime):
    runner = object.__new__(GatewayRunner)
    session_key = _settled_tool_record(runner, lifetime=lifetime)
    monkeypatch.setattr("tools.process_registry.process_registry.has_active_processes", lambda _task: False)
    monkeypatch.setattr("tools.async_delegation.has_live_for_session", lambda **_selectors: False)
    receipt = await runner.get_execution_lifecycle(session_key=session_key, execution_id=_EXECUTION_ID)
    assert receipt["occupancy"] == "unknown"
    assert receipt["tools"] == "unknown"


@pytest.mark.asyncio
async def test_missing_completed_call_or_checker_error_retains_occupancy(monkeypatch):
    runner = object.__new__(GatewayRunner)
    session_key = _settled_tool_record(runner, completed=False)
    monkeypatch.setattr("tools.process_registry.process_registry.has_active_processes", lambda _task: False)
    monkeypatch.setattr("tools.async_delegation.has_live_for_session", lambda **_selectors: False)
    receipt = await runner.get_execution_lifecycle(session_key=session_key, execution_id=_EXECUTION_ID)
    assert receipt["occupancy"] == "unknown"


@pytest.mark.asyncio
async def test_native_lifetime_checker_error_retains_occupancy(monkeypatch):
    runner = object.__new__(GatewayRunner)
    session_key = _settled_tool_record(runner)
    monkeypatch.setattr("tools.process_registry.process_registry.has_active_processes",
                        lambda _task: (_ for _ in ()).throw(RuntimeError("registry unavailable")))
    receipt = await runner.get_execution_lifecycle(session_key=session_key, execution_id=_EXECUTION_ID)
    assert receipt["occupancy"] == "unknown"


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
