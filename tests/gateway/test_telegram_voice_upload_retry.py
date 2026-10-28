"""A transient Telegram voice upload must be retried, not dropped.

Live fleet TTS generated the Ogg and then ``send_voice`` died on
``TimedOut`` / ``httpx.ReadError``. The adapter handed that to the base
sender, which only logs that native audio send is unavailable.
"""
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as telegram_mod
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    adapter._bot.send_voice = AsyncMock(return_value=MagicMock(message_id=1))
    adapter._bot.send_document = AsyncMock(return_value=MagicMock(message_id=2))
    return adapter


def _write_ogg(tmp_path):
    audio = tmp_path / "reply.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 16)
    return audio


@pytest.mark.asyncio
async def test_voice_upload_retries_once_after_read_error(monkeypatch, tmp_path):
    """A ReadError on the first upload is retried, and the retry can deliver the bubble."""
    monkeypatch.setattr(telegram_mod, "_probe_voice_duration_seconds", lambda _p: 2)
    adapter = _make_adapter()
    adapter._bot.send_voice = AsyncMock(
        side_effect=[httpx.ReadError("reset"), MagicMock(message_id=9)]
    )

    result = await adapter.send_voice("123", str(_write_ogg(tmp_path)))

    assert result.success is True
    assert result.message_id == "9"
    assert adapter._bot.send_voice.await_count == 2
    adapter._bot.send_document.assert_not_called()


@pytest.mark.asyncio
async def test_voice_upload_sends_document_when_retry_also_fails(monkeypatch, tmp_path):
    """Two transient upload failures deliver the file as a document, not a dropped warning."""
    monkeypatch.setattr(telegram_mod, "_probe_voice_duration_seconds", lambda _p: 2)
    adapter = _make_adapter()
    adapter._bot.send_voice = AsyncMock(side_effect=httpx.ReadError("reset"))
    adapter.send_document = AsyncMock(return_value=MagicMock(success=True, message_id="44"))

    result = await adapter.send_voice("123", str(_write_ogg(tmp_path)))

    assert result.success is True
    assert result.message_id == "44"
    assert adapter._bot.send_voice.await_count == 2
    adapter.send_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_upload_does_not_retry_a_bot_api_rejection(monkeypatch, tmp_path):
    """A permanent Bot API rejection is not sent twice and is not turned into a document."""
    monkeypatch.setattr(telegram_mod, "_probe_voice_duration_seconds", lambda _p: 2)
    adapter = _make_adapter()
    adapter._bot.send_voice = AsyncMock(side_effect=Exception("Bad Request: VOICE_MESSAGES_FORBIDDEN"))
    adapter.send_document = AsyncMock()

    result = await adapter.send_voice("123", str(_write_ogg(tmp_path)))

    assert adapter._bot.send_voice.await_count == 1
    adapter.send_document.assert_not_called()
    assert result.success is False
