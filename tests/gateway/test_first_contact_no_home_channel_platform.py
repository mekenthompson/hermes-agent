"""A platform that declares ``has_home_channel=False`` never gets the "No home channel, /sethome" nudge (HF-787).

Linear agent sessions run in a human-facing issue thread; telling the issue's participants to set a home
channel is noise. The Linear plugin declares the capability on its ``PlatformEntry``; the nudge for chat
platforms (Telegram, Slack...) is unchanged.
"""

from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore


def _runner(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    for name in ("TELEGRAM_HOME_CHANNEL", "SLACK_HOME_CHANNEL"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text("model: test\n", encoding="utf-8")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = SessionStore(sessions_dir=tmp_path / "sessions", config=runner.config)
    runner._deliver_platform_notice = AsyncMock()
    return runner


@pytest.fixture
def linear_like():
    """A plugin platform the way the Linear plugin registers it: no home-channel concept."""
    entry = PlatformEntry(name="linearlike", label="Linear-like", adapter_factory=lambda cfg: None,
                          check_fn=lambda: True, has_home_channel=False)
    platform_registry.register(entry)
    yield Platform("linearlike")
    platform_registry.unregister("linearlike")


async def _first_contact(runner, platform, chat_type="dm"):
    source = SessionSource(platform=platform, chat_id="room-1", user_id="u1", chat_type=chat_type)
    await runner._hmwa_first_contact_notes(source, [], [])
    return runner._deliver_platform_notice.await_args_list


@pytest.mark.asyncio
async def test_platform_without_a_home_channel_concept_gets_no_nudge(tmp_path, monkeypatch, linear_like):
    assert await _first_contact(_runner(tmp_path, monkeypatch), linear_like, chat_type="thread") == []


@pytest.mark.asyncio
async def test_default_for_a_plugin_platform_is_to_have_one(tmp_path, monkeypatch):
    entry = PlatformEntry(name="chatlike", label="Chat-like", adapter_factory=lambda cfg: None, check_fn=lambda: True)
    assert entry.has_home_channel is True
    platform_registry.register(entry)
    try:
        calls = await _first_contact(_runner(tmp_path, monkeypatch), Platform("chatlike"))
    finally:
        platform_registry.unregister("chatlike")
    assert len(calls) == 1 and "sethome" in calls[0].args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform, command", [(Platform.TELEGRAM, "/sethome"), (Platform.SLACK, "/hermes sethome")])
async def test_telegram_and_slack_are_unchanged(tmp_path, monkeypatch, platform, command):
    calls = await _first_contact(_runner(tmp_path, monkeypatch), platform)
    assert len(calls) == 1 and command in calls[0].args[1]
