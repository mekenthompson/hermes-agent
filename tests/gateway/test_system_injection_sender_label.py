"""Self-injected turns (heartbeat, async-delegation completion) must carry their system header,
never the originating human's shared-session sender label, and a heartbeat drained as a queued
follow-up must still be a scheduled (quiet) heartbeat."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource

HEARTBEAT = "[Heartbeat — recurring instruction, fires every 3m]\nCheck active tracked work."
ASYNC_DONE = "[ASYNC DELEGATION BATCH COMPLETE — deleg_abc123]\nA background fan-out unit finished."


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    return runner


def _slack_source() -> SessionSource:
    return SessionSource(platform=Platform.SLACK, chat_id="C1", chat_type="group", thread_id="171.1",
                         user_id="U123ABC", user_name="Alice")


def _heartbeat_poller_event(source):
    """The run_goals poller shape: non-internal (auth and Stop still apply), marked by session id."""
    event = MessageEvent(text=HEARTBEAT, source=source)
    event._heartbeat_session_id = "20250101_000000_abcd1234"
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("make_event", [
    pytest.param(lambda src: MessageEvent(text=HEARTBEAT, source=src, internal=True), id="heartbeat-admission"),
    pytest.param(_heartbeat_poller_event, id="heartbeat-poller-noninternal"),
    pytest.param(lambda src: MessageEvent(text=ASYNC_DONE, source=src, internal=True), id="async-delegation"),
])
async def test_system_injection_has_no_human_sender_prefix(make_event):
    runner, source = _runner(), _slack_source()
    event = make_event(source)
    model_text = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    _, persisted, _ = runner._hmwa_apply_message_timestamp(event, model_text)
    for text in (model_text, persisted):
        assert "Slack user" not in text and "Alice" not in text
        assert text.startswith(("[Heartbeat", "[ASYNC DELEGATION"))


@pytest.mark.asyncio
async def test_human_message_keeps_sender_prefix():
    runner, source = _runner(), _slack_source()
    event = MessageEvent(text="hello", source=source)
    model_text = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    assert model_text.startswith("[Alice | Slack user <@U123ABC>] hello")


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeat", [True, False])
async def test_queued_followup_keeps_scheduled_heartbeat_flag(heartbeat):
    runner = _runner()
    runner._MAX_INTERRUPT_DEPTH = 8
    runner._run_agent = AsyncMock(return_value={"final_response": "[SILENT]", "messages": []})
    runner._run_agent_deliver_first_response = AsyncMock()
    runner._is_goal_continuation_event = MagicMock(return_value=False)
    runner._session_key_for_source = MagicMock(return_value="agent:main:slack:group:C1")
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value=HEARTBEAT)
    runner._reply_anchor_for_event = MagicMock(return_value=None)
    runner._delivery_adapter_for = MagicMock(return_value=None)
    runner._refresh_agent_cache_message_count = AsyncMock()
    source = _slack_source()
    turn_ctx = SimpleNamespace(
        source=source, session_id="sid", session_key="agent:main:slack:group:C1", run_generation=1,
        _interrupt_depth=0, history=[], _status_thread_metadata=None, context_prompt=None,
        result_holder=[None], chat_execution_reservation=None,
    )
    pending_event = SimpleNamespace(
        source=source, message_id="hb", channel_prompt=None, message_type=None, internal=True, metadata={},
        reply_expected=None,
    )
    if heartbeat:
        pending_event._heartbeat_session_id = "sid"

    await GatewayRunner._run_agent_queued_followup(
        runner, turn_ctx, adapter=None, pending=HEARTBEAT, pending_event=pending_event,
        response="resp", result={"interrupted": True, "messages": []}, stream_task=None,
    )
    assert runner._run_agent.await_args.kwargs["scheduled_heartbeat"] is heartbeat
