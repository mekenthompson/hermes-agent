"""Telegram voice uploads recover from transport failures without duplicating the reply.

Live fleet TTS generated the Ogg and then ``send_voice`` died on ``TimedOut`` / ``httpx.ReadError``.
An earlier fix retried every such failure once and then resent the file as a document. After a
Telegram Bad Gateway outage the first upload had usually landed already, so one reply arrived as two
or three voice notes (HF-672). The contract now:

* failure before the request left (connect or pool timeout): retry once, then a document fallback;
* failure after the request may have reached Telegram (read/write timeout, read error, network
  error, wall-clock deadline): no retry and no document, an unconfirmed failure instead;
* Bot API rejection: no retry, no document.
"""
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from telegram.error import NetworkError, TimedOut

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as telegram_mod
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.adapter_voice_upload import VOICE_UPLOAD_UNCONFIRMED


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    adapter._bot.send_voice = AsyncMock(return_value=MagicMock(message_id=1))
    adapter._bot.send_document = AsyncMock(return_value=MagicMock(message_id=2))
    adapter.send_document = AsyncMock(return_value=MagicMock(success=True, message_id="44"))
    return adapter


def _write_ogg(tmp_path):
    audio = tmp_path / "reply.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 16)
    return audio


def _wrapped(outer: Exception, cause: Exception) -> Exception:
    outer.__cause__ = cause
    return outer


@pytest.fixture(autouse=True)
def _no_ffprobe(monkeypatch):
    monkeypatch.setattr(telegram_mod, "_probe_voice_duration_seconds", lambda _p: 2)


class _TelegramServer:
    """Fake Bot API ``sendVoice``. Each scripted attempt says whether Telegram accepted the upload
    and which error (if any) the client then saw. ``landed`` counts copies the user would see."""

    def __init__(self, attempts):
        self.attempts = list(attempts)
        self.landed = 0
        self.calls = 0

    async def send_voice(self, **_kwargs):
        self.calls += 1
        error, accepted = self.attempts.pop(0) if self.attempts else (None, True)
        if accepted:
            self.landed += 1
        if error is not None:
            raise error
        return MagicMock(message_id=100 + self.calls)


def _read_timeout():
    return _wrapped(TimedOut("Timed out"), httpx.ReadTimeout("read timed out"))


def _connect_timeout():
    return _wrapped(TimedOut("Timed out"), httpx.ConnectTimeout("connect timed out"))


@pytest.mark.asyncio
@pytest.mark.parametrize("make_error", [
    _read_timeout,
    lambda: _wrapped(TimedOut("Timed out"), httpx.WriteTimeout("write timed out")),
    lambda: httpx.ReadError("connection reset"),
    lambda: NetworkError("Bad Gateway"),
    lambda: TimeoutError("timed out after 120s (telegram-media-send)"),
], ids=["read-timeout", "write-timeout", "read-error", "bad-gateway", "deadline"])
async def test_unconfirmed_upload_is_not_resent(tmp_path, make_error):
    """The upload landed but the response was lost: the user gets exactly one copy."""
    adapter = _make_adapter()
    server = _TelegramServer([(make_error(), True)])
    adapter._bot.send_voice = AsyncMock(side_effect=server.send_voice)

    result = await adapter.send_voice("123", str(_write_ogg(tmp_path)))

    assert server.calls == 1
    assert server.landed == 1
    adapter.send_document.assert_not_called()
    assert result.success is False
    assert result.retryable is False
    assert result.error == VOICE_UPLOAD_UNCONFIRMED


@pytest.mark.asyncio
@pytest.mark.parametrize("make_error", [
    _connect_timeout,
    lambda: _wrapped(TimedOut("Pool timeout: All connections in the connection pool are occupied."),
                     httpx.PoolTimeout("pool")),
], ids=["connect-timeout", "pool-timeout"])
async def test_never_sent_upload_retries_once(tmp_path, make_error):
    """The request never left: one retry delivers the bubble, and only one copy exists."""
    adapter = _make_adapter()
    server = _TelegramServer([(make_error(), False)])
    adapter._bot.send_voice = AsyncMock(side_effect=server.send_voice)

    result = await adapter.send_voice("123", str(_write_ogg(tmp_path)))

    assert result.success is True
    assert result.message_id == "102"
    assert server.calls == 2
    assert server.landed == 1
    adapter.send_document.assert_not_called()


@pytest.mark.asyncio
async def test_never_sent_twice_falls_back_to_one_document(tmp_path):
    """Neither attempt reached Telegram, so the document fallback is the only copy."""
    adapter = _make_adapter()
    server = _TelegramServer([(_connect_timeout(), False), (_connect_timeout(), False)])
    adapter._bot.send_voice = AsyncMock(side_effect=server.send_voice)

    result = await adapter.send_voice("123", str(_write_ogg(tmp_path)))

    assert result.success is True
    assert result.message_id == "44"
    assert server.calls == 2
    assert server.landed == 0
    adapter.send_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_that_lands_unconfirmed_gets_no_document(tmp_path):
    """First attempt never sent; the retry landed but its response timed out: no document copy."""
    adapter = _make_adapter()
    server = _TelegramServer([(_connect_timeout(), False), (_read_timeout(), True)])
    adapter._bot.send_voice = AsyncMock(side_effect=server.send_voice)

    result = await adapter.send_voice("123", str(_write_ogg(tmp_path)))

    assert server.calls == 2
    assert server.landed == 1
    adapter.send_document.assert_not_called()
    assert result.success is False
    assert result.error == VOICE_UPLOAD_UNCONFIRMED


@pytest.mark.asyncio
async def test_voice_upload_does_not_retry_a_bot_api_rejection(tmp_path):
    """A permanent Bot API rejection is not sent twice and is not turned into a document."""
    adapter = _make_adapter()
    adapter._bot.send_voice = AsyncMock(side_effect=Exception("Bad Request: VOICE_MESSAGES_FORBIDDEN"))

    result = await adapter.send_voice("123", str(_write_ogg(tmp_path)))

    assert adapter._bot.send_voice.await_count == 1
    adapter.send_document.assert_not_called()
    assert result.success is False
