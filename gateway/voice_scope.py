"""Topic scoping for ``/voice`` and adapter auto-TTS (sibling of ``run_voice.py``).

Telegram DM and forum topics are separate conversations, so one ``/voice tts`` must not make every
topic speak. The same scope id keys the runner's persisted ``/voice`` mode (after the
``<platform>:`` prefix) and the adapter's auto-TTS enabled/disabled sets, so a restart restores
per-topic state. Other platforms keep chat-level scope.
"""

from __future__ import annotations

from typing import Any, Optional

TOPIC_SCOPED_VOICE_PLATFORMS = frozenset({"telegram"})


def voice_scope_id(platform: Any, chat_id: Any, thread_id: Optional[str] = None) -> str:
    """``<chat_id>:<thread_id>`` for a topic on a topic-scoped platform, else ``<chat_id>``."""
    thread = str(thread_id).strip() if thread_id is not None else ""
    if thread and getattr(platform, "value", platform) in TOPIC_SCOPED_VOICE_PLATFORMS:
        return f"{chat_id}:{thread}"
    return str(chat_id)


def should_auto_tts_for_source(adapter: Any, source: Any) -> bool:
    """Topic-aware ``adapter._should_auto_tts_for_chat``: an explicit topic ``/voice`` choice
    wins, else the chat-level decision applies."""
    chat_id = str(source.chat_id)
    scoped = voice_scope_id(source.platform, chat_id, getattr(source, "thread_id", None))
    if scoped != chat_id:
        if scoped in adapter._auto_tts_enabled_chats:
            return True
        if scoped in adapter._auto_tts_disabled_chats:
            return False
    return bool(adapter._should_auto_tts_for_chat(chat_id))
