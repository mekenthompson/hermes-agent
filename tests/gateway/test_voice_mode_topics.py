"""``/voice`` is scoped to the Telegram forum topic it is sent in; the chat-level mode is the fallback.

Telegram DM and forum topics are separate conversations. ``/voice tts`` was keyed
``telegram:<chat_id>`` only, so one command made every topic speak every reply.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource

CHAT = "1002003004"


def _runner(tmp_path):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._VOICE_MODE_PATH = tmp_path / "gateway_voice_mode.json"
    runner._session_db = None
    runner._is_user_authorized = lambda source: True
    return runner


def _event(text="", *, thread_id=None, platform=Platform.TELEGRAM, message_type=MessageType.TEXT):
    source = SessionSource(platform=platform, chat_id=CHAT, chat_type="dm", user_id="u1", thread_id=thread_id)
    event = MessageEvent(text=text, message_type=message_type, source=source)
    event.message_id = "m1"
    return event


def _speaks(runner, thread_id=None, message_type=MessageType.TEXT):
    return runner._should_send_voice_reply(_event(thread_id=thread_id, message_type=message_type), "Hello!", [])


def _adapter():
    adapter = SimpleNamespace(
        _auto_tts_default=False, _auto_tts_enabled_chats=set(), _auto_tts_disabled_chats=set(),
        platform=Platform.TELEGRAM)
    adapter._should_auto_tts_for_chat = lambda chat_id: BasePlatformAdapter._should_auto_tts_for_chat(adapter, chat_id)
    return adapter


@pytest.mark.asyncio
async def test_voice_tts_in_a_topic_sets_only_that_topic(tmp_path):
    runner = _runner(tmp_path)

    await runner._handle_voice_command(_event("/voice tts", thread_id="10"))

    assert runner._voice_mode == {f"telegram:{CHAT}:10": "all"}
    assert _speaks(runner, thread_id="10") is True
    assert _speaks(runner, thread_id="20") is False
    assert _speaks(runner, thread_id=None) is False


@pytest.mark.asyncio
async def test_voice_on_and_off_are_per_topic(tmp_path):
    runner = _runner(tmp_path)

    await runner._handle_voice_command(_event("/voice on", thread_id="10"))
    await runner._handle_voice_command(_event("/voice tts", thread_id="20"))
    await runner._handle_voice_command(_event("/voice off", thread_id="20"))

    assert runner._voice_mode == {f"telegram:{CHAT}:10": "voice_only", f"telegram:{CHAT}:20": "off"}


@pytest.mark.asyncio
async def test_unset_topic_inherits_chat_mode_and_topic_choice_wins(tmp_path):
    runner = _runner(tmp_path)
    await runner._handle_voice_command(_event("/voice tts"))  # outside any topic: chat level
    await runner._handle_voice_command(_event("/voice off", thread_id="10"))

    assert runner._voice_mode == {f"telegram:{CHAT}": "all", f"telegram:{CHAT}:10": "off"}
    assert _speaks(runner, thread_id="20") is True
    assert _speaks(runner, thread_id="10") is False
    assert _speaks(runner) is True


@pytest.mark.asyncio
async def test_status_and_toggle_read_the_effective_topic_mode(tmp_path):
    from agent.i18n import t
    runner = _runner(tmp_path)
    runner._voice_mode = {f"telegram:{CHAT}": "voice_only"}

    status = await runner._handle_voice_command(_event("/voice status", thread_id="10"))
    assert t("gateway.voice.label_voice_only") in status

    await runner._handle_voice_command(_event("/voice", thread_id="10"))  # toggles inherited on -> off
    assert runner._voice_mode == {f"telegram:{CHAT}": "voice_only", f"telegram:{CHAT}:10": "off"}


@pytest.mark.asyncio
async def test_topic_modes_survive_restart_and_reach_the_adapter(tmp_path):
    runner = _runner(tmp_path)
    await runner._handle_voice_command(_event("/voice tts", thread_id="10"))
    await runner._handle_voice_command(_event("/voice off", thread_id="20"))
    await runner._handle_voice_command(_event("/voice on"))

    restarted = _runner(tmp_path)
    restarted._voice_mode = restarted._load_voice_modes()
    assert _speaks(restarted, thread_id="10") is True
    assert _speaks(restarted, thread_id="20") is False

    adapter = _adapter()
    with patch("hermes_cli.config.load_config", return_value={}):
        restarted._sync_voice_mode_state_to_adapter(adapter)
    from gateway.voice_scope import should_auto_tts_for_source

    def voice_in(thread_id):
        return _event(thread_id=thread_id, message_type=MessageType.VOICE).source
    assert should_auto_tts_for_source(adapter, voice_in("10")) is True
    assert should_auto_tts_for_source(adapter, voice_in("20")) is False  # topic off beats chat on
    assert should_auto_tts_for_source(adapter, voice_in("30")) is True  # inherits chat voice_only


@pytest.mark.asyncio
async def test_live_adapter_auto_tts_is_per_topic(tmp_path):
    from gateway.voice_scope import should_auto_tts_for_source
    runner = _runner(tmp_path)
    adapter = _adapter()
    runner.adapters[Platform.TELEGRAM] = adapter

    await runner._handle_voice_command(_event("/voice on", thread_id="10"))

    assert should_auto_tts_for_source(adapter, _event(thread_id="10").source) is True
    assert should_auto_tts_for_source(adapter, _event(thread_id="20").source) is False
    assert adapter._should_auto_tts_for_chat(CHAT) is False


@pytest.mark.asyncio
async def test_non_telegram_threads_keep_chat_scope(tmp_path):
    runner = _runner(tmp_path)

    await runner._handle_voice_command(_event("/voice tts", thread_id="1700000000.1", platform=Platform.SLACK))

    assert runner._voice_mode == {f"slack:{CHAT}": "all"}
