"""Contract tests for profile-local plugin turns through GatewayRunner."""

from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _event(**overrides) -> MessageEvent:
    source = overrides.pop(
        "source",
        SessionSource(
            platform=Platform.LOCAL,
            chat_id="linear-session-1",
            chat_name="Linear Agent Session",
            chat_type="dm",
            user_id="linear-agent",
            user_name="Linear Agent",
            scope_id="workspace-1",
            profile="aggie",
        ),
    )
    values = {
        "text": "untrusted payload",
        "message_type": MessageType.TEXT,
        "source": source,
        "internal": True,
        "allow_gateway_control": False,
        "metadata": {"linear_agent": True},
    }
    values.update(overrides)
    return MessageEvent(**values)


@pytest.mark.asyncio
async def test_dispatch_internal_plugin_event_uses_normal_scoped_handler():
    runner = object.__new__(GatewayRunner)
    handler = AsyncMock(return_value="agent response")
    runner._primary_message_handler = lambda: handler
    event = _event()

    result = await runner.dispatch_internal_plugin_event(event)

    assert result == "agent response"
    handler.assert_awaited_once_with(event)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "error"),
    [
        (object(), TypeError),
        (_event(internal=False), PermissionError),
        (_event(allow_gateway_control=True), PermissionError),
        (_event(message_type=MessageType.PHOTO), ValueError),
        (_event(source=None), ValueError),
        (
            _event(
                source=SessionSource(
                    platform=Platform.SLACK,
                    chat_id="C123",
                    chat_type="dm",
                    profile="aggie",
                )
            ),
            PermissionError,
        ),
        (
            _event(
                source=SessionSource(
                    platform=Platform.LOCAL,
                    chat_id="local-1",
                    chat_type="dm",
                    profile=None,
                )
            ),
            ValueError,
        ),
    ],
)
async def test_dispatch_internal_plugin_event_rejects_unsafe_shapes(event, error):
    runner = object.__new__(GatewayRunner)
    handler = AsyncMock(return_value="must not run")
    runner._primary_message_handler = lambda: handler

    with pytest.raises(error):
        await runner.dispatch_internal_plugin_event(event)

    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_internal_plugin_event_keeps_slash_payload_as_text():
    runner = object.__new__(GatewayRunner)
    handler = AsyncMock(return_value="treated as conversation")
    runner._primary_message_handler = lambda: handler
    event = _event(text="/restart")

    result = await runner.dispatch_internal_plugin_event(event)

    assert result == "treated as conversation"
    assert event.is_command() is False
    handler.assert_awaited_once_with(event)
