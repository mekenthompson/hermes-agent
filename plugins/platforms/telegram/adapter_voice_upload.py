"""Telegram voice upload with duplicate-safe transport recovery.

A voice upload is retried (and then sent as a document) only when the request provably never
reached Telegram: a connect or pool timeout. A read/write timeout, wall-clock deadline or other
mid-request network failure leaves the outcome unknown; Telegram has usually accepted the upload
already, so resending produced two or three copies of one reply (HF-672). Those fail without a
resend.

Lives outside ``adapter.py`` so the file-size ratchet can only go down.
``send_voice`` late-imports this module.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

VOICE_UPLOAD_UNCONFIRMED = "voice upload outcome unconfirmed after a transport failure; not resent"


def voice_upload_never_sent(adapter, error: Exception) -> bool:
    """True when the upload failed before the request left: re-sending cannot duplicate."""
    if adapter._is_bad_request_error(error) or adapter._looks_like_auth_error(error):
        return False
    return bool(adapter._looks_like_connect_timeout(error) or adapter._looks_like_pool_timeout(error))


def voice_upload_outcome_unknown(adapter, error: Exception) -> bool:
    """True when a transport failure hit after the request may have reached Telegram.

    A Bot API rejection or auth failure is a definite failure, not an unknown outcome.
    """
    from plugins.platforms.telegram.adapter import _iter_exception_graph

    if adapter._is_bad_request_error(error) or adapter._looks_like_auth_error(error):
        return False
    if voice_upload_never_sent(adapter, error):
        return False
    markers = ("readerror", "writeerror", "readtimeout", "writetimeout", "networkerror", "timedout",
               "timeouterror")
    for cur in _iter_exception_graph(error):
        name = cur.__class__.__name__.lower()
        if name in {"badrequest", "forbidden", "invalidtoken", "retryafter"}:
            return False
        if any(marker in name for marker in markers) or "timed out" in str(cur).lower():
            return True
    return False


async def upload_voice_file(
    adapter, chat_id: str, audio_path: str, caption: Optional[str], reply_to: Optional[str],
    metadata: Optional[dict[str, Any]]):
    """Upload one already-resolved audio file. Raises on transport failure."""
    from plugins.platforms.telegram import adapter as telegram_mod
    from gateway.platforms.base import SendResult
    import asyncio

    duration_secs = await asyncio.to_thread(
        telegram_mod._probe_voice_duration_seconds, audio_path)
    # health: allow ASYNC230 -- streamed to the Bot API upload; a local open is non-blocking in practice
    with open(audio_path, "rb") as audio_file:  # noqa: ASYNC230
        ext = os.path.splitext(audio_path)[1].lower()
        if ext in {".ogg", ".opus"}:
            msg = await adapter._send_voice_bubble(
                audio_file, chat_id, reply_to, metadata, caption, duration_secs)
        elif ext in {".mp3", ".m4a"}:
            msg = await adapter._send_media(
                adapter._bot.send_audio, chat_id, reply_to, metadata, "audio",
                reset_media=lambda: audio_file.seek(0),
                audio=audio_file, caption=adapter._caption_1024(caption), duration=duration_secs)
        else:
            return await adapter.send_document(
                chat_id=chat_id, file_path=audio_path, caption=caption, reply_to=reply_to, metadata=metadata)
    return SendResult(success=True, message_id=str(msg.message_id))


async def deliver_voice(
    adapter, chat_id: str, audio_path: str, caption: Optional[str] = None,
    reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None, **kwargs):
    """Send a voice bubble. Retry once, then fall back to a document, only when nothing was sent;
    an unconfirmed upload fails without a resend so the user gets at most one copy."""
    from gateway.platforms.base import SendResult
    from plugins.platforms.telegram.adapter import TelegramAdapter, _redact_telegram_error_text

    if not adapter._bot:
        return SendResult(success=False, error="Not connected")
    import asyncio
    if not await asyncio.to_thread(os.path.exists, audio_path):
        return SendResult(success=False, error=adapter._missing_media_path_error("Audio", audio_path))
    upload_path = audio_path
    transcoded: Optional[str] = None
    try:
        if kwargs.get("is_voice") and os.path.splitext(audio_path)[1].lower() not in (".ogg", ".opus"):
            from gateway.platforms.base import transcode_to_ogg_opus
            import asyncio
            transcoded = await asyncio.to_thread(transcode_to_ogg_opus, audio_path)
            if transcoded:
                upload_path = transcoded
            else:
                logger.warning(
                    "[%s] voice transcode unavailable for %s — sending original format (install ffmpeg for voice bubbles)",
                    adapter.name, os.path.basename(audio_path))
        # Each attempt's failure is classified on its own. The retry runs outside the ``except``
        # block so its error does not chain the first one as ``__context__``: a landed-but-timed-out
        # retry must not read as "never sent" because the first attempt's connect timed out.
        error: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                return await upload_voice_file(adapter, chat_id, upload_path, caption, reply_to, metadata)
            except Exception as exc:  # health: allow BLE001 -- classified here; the final one is re-raised
                error = exc
            logger.warning(
                "[%s] Voice upload attempt %d failed (%s)",
                adapter.name, attempt, _redact_telegram_error_text(error))
            if attempt == 1 and voice_upload_never_sent(adapter, error):
                logger.warning("[%s] Voice upload never reached Telegram; retrying once", adapter.name)
                continue
            break
        raise error or RuntimeError("Telegram voice upload failed")
    except Exception as error:
        logger.warning(
            "[%s] Voice delivery failed (%s)", adapter.name, _redact_telegram_error_text(error), exc_info=True)
        if voice_upload_never_sent(adapter, error):
            logger.warning("[%s] Sending the voice file as a document after the retry failed", adapter.name)
            return await adapter.send_document(
                chat_id=chat_id, file_path=upload_path, caption=caption, reply_to=reply_to, metadata=metadata)
        if voice_upload_outcome_unknown(adapter, error):
            logger.warning(
                "[%s] Voice upload may already have landed; not resending (no retry, no document)",
                adapter.name)
            return SendResult(success=False, error=VOICE_UPLOAD_UNCONFIRMED, retryable=False,
                              error_kind="unknown")
        return await super(TelegramAdapter, adapter).send_voice(
            chat_id, audio_path, caption, reply_to, metadata=metadata)
    finally:
        if transcoded:
            with contextlib.suppress(OSError):
                os.unlink(transcoded)
