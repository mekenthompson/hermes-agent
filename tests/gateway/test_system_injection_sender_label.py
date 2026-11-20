"""Gateway-made turns (heartbeat, background-process and async-delegation completions) must carry
their own system header, never the shared-session sender label of the human whose message armed them.

The stored/rebuilt source of such an event keeps the originating human's ``user_name``
(``_build_process_event_source`` copies it from the watcher route), so a label applied from
``source`` alone would attribute machine text to a person.
"""

from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource

HEARTBEAT = "[Heartbeat - recurring instruction, fires every 3m]\nCheck active tracked work."
ASYNC_DONE = "[ASYNC DELEGATION BATCH COMPLETE - deleg_abc123]\nA background fan-out unit finished."
PROCESS_DONE = "[SYSTEM: Background process proc_1 finished with exit code 0]"


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


_SYSTEM_EVENTS = [
    pytest.param(lambda src: MessageEvent(text=HEARTBEAT, source=src, internal=True), id="heartbeat-admission"),
    pytest.param(_heartbeat_poller_event, id="heartbeat-poller-noninternal"),
    pytest.param(lambda src: MessageEvent(text=ASYNC_DONE, source=src, internal=True), id="async-delegation"),
    pytest.param(lambda src: MessageEvent(text=PROCESS_DONE, source=src, internal=True), id="process-completion"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("make_event", _SYSTEM_EVENTS)
async def test_turn_opening_system_injection_has_no_human_sender_prefix(make_event):
    runner, source = _runner(), _slack_source()
    event = make_event(source)
    model_text = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    _, persisted, _ = runner._hmwa_apply_message_timestamp(event, model_text)
    for text in (model_text, persisted):
        assert "Slack user" not in text and "Alice" not in text
        assert text.startswith(("[Heartbeat", "[ASYNC DELEGATION", "[SYSTEM"))


@pytest.mark.parametrize("make_event", _SYSTEM_EVENTS)
def test_mid_turn_steer_of_a_system_injection_has_no_human_sender_prefix(make_event):
    runner, source = _runner(), _slack_source()
    runner._profile_scope_for_source = lambda src: _NullScope()
    text = runner._steer_text_with_origin(make_event(source).text, make_event(source))
    assert "Slack user" not in text and "Alice" not in text


class _NullScope:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_process_watcher_event_really_carries_the_human_name_but_gets_no_label():
    runner = _runner()
    runner.session_store = SimpleNamespace(_entries={})
    evt = {"type": "completion", "platform": "slack", "chat_type": "group", "chat_id": "C1",
           "thread_id": "171.1", "user_id": "U123ABC", "user_name": "Alice", "scope_id": "T1"}
    source = runner._build_process_event_source(evt)
    assert source is not None and source.user_name == "Alice"  # the premise: internal source holds a human name

    event = MessageEvent(text=PROCESS_DONE, source=source, internal=True)
    model_text = await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert "Alice" not in model_text and "Slack user" not in model_text


@pytest.mark.asyncio
async def test_human_message_keeps_sender_prefix():
    runner, source = _runner(), _slack_source()
    event = MessageEvent(text="hello", source=source)
    model_text = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    assert model_text.startswith("[Alice | Slack user <@U123ABC>] hello")
