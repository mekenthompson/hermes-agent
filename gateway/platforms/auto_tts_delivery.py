"""Auto-TTS audio delivery helpers for ``BasePlatformAdapter`` (kept out of the facade).

Reply text is always sent before voice audio so synthesis/upload latency never delays it. The
only exception is Telegram, where the first audio file carries the reply text as its caption
and the separate text send is skipped (one text copy either way).
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFERRED_VOICE_ATTR = "_post_text_voice_reply"

# Platforms whose /voice mode is scoped to a topic inside a chat, with the chat-level mode as
# fallback. Telegram DM and forum topics are separate conversations; one ``/voice tts`` must not
# make every topic speak (HF-672). Other platforms keep chat-level scope.
TOPIC_SCOPED_VOICE_PLATFORMS = frozenset({"telegram"})


def voice_scope_id(platform: Any, chat_id: str, thread_id: Optional[str] = None) -> str:
    """``<chat_id>:<thread_id>`` for a topic on a topic-scoped platform, else ``<chat_id>``.

    The same id keys the runner's persisted ``/voice`` mode (after the ``<platform>:`` prefix)
    and the adapter's auto-TTS enabled/disabled sets, so a restart restores per-topic state.
    """
    thread = str(thread_id).strip() if thread_id is not None else ""
    if thread and getattr(platform, "value", platform) in TOPIC_SCOPED_VOICE_PLATFORMS:
        return f"{chat_id}:{thread}"
    return str(chat_id)


def should_auto_tts_for_source(adapter: Any, source: Any) -> bool:
    """Topic-aware ``adapter._should_auto_tts_for_chat``: an explicit topic ``/voice`` choice
    wins, else the chat-level decision applies."""
    chat_id = source.chat_id
    scoped = voice_scope_id(source.platform, chat_id, getattr(source, "thread_id", None))
    if scoped != chat_id:
        if scoped in adapter._auto_tts_enabled_chats:
            return True
        if scoped in adapter._auto_tts_disabled_chats:
            return False
    return bool(adapter._should_auto_tts_for_chat(chat_id))


async def run_auto_tts(
    adapter: Any, event: Any, text_content: str, metadata: dict, record_delivery: Callable,
) -> tuple[bool, list[str]]:
    """Synthesize and play auto-TTS for ``text_content``; always removes generated files.

    Returns ``(caption_delivered, played_paths)``. Never raises: TTS problems are logged so the
    text reply is unaffected.
    """
    caption_delivered, played = False, []
    paths, requested = await adapter._synthesize_auto_tts(text_content)
    try:
        for index, path in enumerate(paths):
            try:
                caption_delivered |= await adapter._play_tts_file(
                    event, text_content, path, index == 0, metadata, record_delivery)
                played.append(path)
            except Exception as exc:  # health: allow BLE001 -- audio is best-effort; text already sent
                logger.warning("[%s] Auto-TTS playback failed: %s", adapter.name, exc)
            finally:
                with contextlib.suppress(OSError):
                    os.remove(path)
    finally:
        if not paths and requested is not None:
            with contextlib.suppress(OSError):
                os.remove(requested)
    return caption_delivered, played


async def run_deferred_voice_reply(event: Any) -> None:
    """Await the runner's voice reply stashed on ``event`` (set when the text is still unsent).

    Called after the text send so voice synthesis never delays the reply; errors are logged.
    """
    deferred = getattr(event, DEFERRED_VOICE_ATTR, None)
    if deferred is None:
        return
    setattr(event, DEFERRED_VOICE_ATTR, None)
    try:
        await deferred()
    except Exception as exc:  # health: allow BLE001 -- audio is best-effort; text already sent
        logger.warning("Deferred auto voice reply failed: %s", exc, exc_info=True)
