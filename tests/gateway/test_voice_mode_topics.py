"""``/voice`` is scoped to the Telegram topic it is sent in, with the chat-level mode as fallback.

Ken's Telegram DM uses topics. ``/voice tts`` was keyed ``telegram:<chat_id>`` only, so one command
made every topic speak every reply (HF-672). A topic now keys ``telegram:<chat_id>:<thread_id>``;
an unset topic inherits the chat-level mode, and with neither set it stays silent (``voice.auto_tts``
default off).
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.auto_tts_delivery import should_auto_tts_for_source, voice_scope_id
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource


CHAT = "8248703757"


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
    adapter._should_auto_tts_for_source = lambda source: should_auto_tts_for_source(adapter, source)
    return adapter


def test_voice_scope_id_only_scopes_telegram_topics():
    assert voice_scope_id(Platform.TELEGRAM, CHAT, "10") == f"{CHAT}:10"
    assert voice_scope_id(Platform.TELEGRAM, CHAT, None) == CHAT
    assert voice_scope_id(Platform.TELEGRAM, CHAT, "") == CHAT
    assert voice_scope_id(Platform.SLACK, "C1", "1700000000.1") == "C1"
    assert voice_scope_id(Platform.DISCORD, "123", "456") == "123"


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
async def test_unset_topic_falls_back_to_chat_mode_and_topic_choice_wins(tmp_path):
    runner = _runner(tmp_path)
    await runner._handle_voice_command(_event("/voice tts"))  # outside any topic: chat level
    await runner._handle_voice_command(_event("/voice off", thread_id="10"))

    assert runner._voice_mode == {f"telegram:{CHAT}": "all", f"telegram:{CHAT}:10": "off"}
    assert _speaks(runner, thread_id="20") is True  # unset topic inherits the chat level
    assert _speaks(runner, thread_id="10") is False  # explicit topic off beats chat-level all
    assert _speaks(runner) is True


@pytest.mark.asyncio
async def test_voice_status_and_toggle_read_the_effective_topic_mode(tmp_path):
    runner = _runner(tmp_path)
    runner._voice_mode = {f"telegram:{CHAT}": "voice_only"}

    status = await runner._handle_voice_command(_event("/voice status", thread_id="10"))
    from agent.i18n import t
    assert t("gateway.voice.label_voice_only") in status

    # Bare /voice toggles from the effective (inherited) mode and writes the topic key only.
    await runner._handle_voice_command(_event("/voice", thread_id="10"))
    assert runner._voice_mode == {f"telegram:{CHAT}": "voice_only", f"telegram:{CHAT}:10": "off"}


@pytest.mark.asyncio
async def test_topic_modes_persist_across_restart(tmp_path):
    runner = _runner(tmp_path)
    await runner._handle_voice_command(_event("/voice tts", thread_id="10"))
    await runner._handle_voice_command(_event("/voice off", thread_id="20"))
    await runner._handle_voice_command(_event("/voice on"))

    saved = json.loads(runner._VOICE_MODE_PATH.read_text())
    assert saved == {f"telegram:{CHAT}:10": "all", f"telegram:{CHAT}:20": "off", f"telegram:{CHAT}": "voice_only"}

    restarted = _runner(tmp_path)
    restarted._voice_mode = restarted._load_voice_modes()
    assert restarted._voice_mode == saved
    assert _speaks(restarted, thread_id="10") is True
    assert _speaks(restarted, thread_id="20") is False
    assert _speaks(restarted, thread_id="30") is False  # chat voice_only, text input

    adapter = _adapter()
    with patch("hermes_cli.config.load_config", return_value={}):
        restarted._sync_voice_mode_state_to_adapter(adapter)
    assert adapter._auto_tts_enabled_chats == {f"{CHAT}:10", CHAT}
    assert adapter._auto_tts_disabled_chats == {f"{CHAT}:20"}

    def voice_in(thread_id):
        return _event(thread_id=thread_id, message_type=MessageType.VOICE).source
    assert adapter._should_auto_tts_for_source(voice_in("10")) is True
    assert adapter._should_auto_tts_for_source(voice_in("20")) is False  # topic off beats chat on
    assert adapter._should_auto_tts_for_source(voice_in("30")) is True  # inherits chat voice_only


@pytest.mark.asyncio
async def test_no_mode_anywhere_keeps_new_topics_silent(tmp_path):
    runner = _runner(tmp_path)
    assert _speaks(runner, thread_id="10") is False
    assert _speaks(runner, thread_id="10", message_type=MessageType.VOICE) is False
    assert _adapter()._should_auto_tts_for_source(_event(thread_id="10").source) is False


@pytest.mark.asyncio
async def test_command_updates_the_live_adapter_topic_entry(tmp_path):
    runner = _runner(tmp_path)
    adapter = _adapter()
    runner.adapters[Platform.TELEGRAM] = adapter

    await runner._handle_voice_command(_event("/voice on", thread_id="10"))

    assert adapter._auto_tts_enabled_chats == {f"{CHAT}:10"}
    assert adapter._should_auto_tts_for_source(_event(thread_id="10").source) is True
    assert adapter._should_auto_tts_for_source(_event(thread_id="20").source) is False
    assert adapter._should_auto_tts_for_chat(CHAT) is False


@pytest.mark.asyncio
async def test_non_telegram_threads_keep_chat_scope(tmp_path):
    runner = _runner(tmp_path)

    await runner._handle_voice_command(_event("/voice tts", thread_id="1700000000.1", platform=Platform.SLACK))

    assert runner._voice_mode == {f"slack:{CHAT}": "all"}


@pytest.mark.asyncio
async def test_voice_reply_is_sent_to_its_originating_topic(tmp_path):
    runner = _runner(tmp_path)
    adapter = AsyncMock()
    runner.adapters[Platform.TELEGRAM] = adapter
    await runner._handle_voice_command(_event("/voice tts", thread_id="10"))
    event = _event(thread_id="10")

    with patch("tools.tts_tool.text_to_speech_tool", return_value=json.dumps({"success": True, "file_path": "/x.ogg"})), \
         patch("tools.tts_text_normalize._strip_markdown_for_tts", side_effect=lambda text: text), \
         patch("os.path.isfile", return_value=True), patch("os.unlink"):
        await runner._send_voice_reply(event, "Hello")

    adapter.send_voice.assert_awaited_once()
    kwargs = adapter.send_voice.await_args.kwargs
    assert kwargs["chat_id"] == CHAT
    assert kwargs["metadata"]["thread_id"] == "10"
