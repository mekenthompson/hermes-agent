"""Adapter processing hooks, including durable input acceptance and presentation."""
from __future__ import annotations

import asyncio
from typing import Any

from gateway.platforms.event import MessageEvent, ProcessingOutcome


class ProcessingLifecycleMixin:
    # Adapters exposing reaction methods can opt into processing-complete reactions.
    _ACK_EMOJI: str | None = None
    _OK_EMOJI: str | None = None
    _FAIL_EMOJI: str | None = None

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Hook called when background processing begins."""

    async def on_message_admitted(self, event: MessageEvent, receipt) -> None:
        """A native owner committed this input (or resolved its exact retry).

        A transport may now ACK its durable inbox. This receipt proves acceptance,
        not model completion, effect completion or publication of the final reply.
        """

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Finish processing; opt-in reactions replace the in-progress reaction.

        Remove-then-add works whether a platform replaces or stacks a sender's
        reactions. CANCELLED leaves the message unreacted.
        """
        if self._OK_EMOJI is None and self._FAIL_EMOJI is None:
            return
        add: Any = getattr(self, "_add_reaction", None)
        remove: Any = getattr(self, "_remove_reaction", None)
        enabled = getattr(self, "_reactions_enabled", None)
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if (not callable(add) or not callable(remove) or (callable(enabled) and not enabled())
                or not chat_id or not message_id):
            return
        await remove(chat_id, message_id)
        emoji = {ProcessingOutcome.SUCCESS: self._OK_EMOJI,
                 ProcessingOutcome.FAILURE: self._FAIL_EMOJI}.get(outcome)
        if emoji:
            await add(chat_id, message_id, emoji)

    async def _drain_pending_after_session_command(
        self, session_key: str, command_guard: asyncio.Event) -> None:
        """Tail of /stop, /new, /reset: release the command-scoped guard, then
        spawn a fresh processing task for any follow-up queued meanwhile."""
        await self._flush_text_debounce_now(session_key)
        pending_event = self._pending_messages.pop(session_key, None)
        self._release_session_guard(session_key, guard=command_guard)
        if pending_event is not None:
            self._start_session_processing(pending_event, session_key)

