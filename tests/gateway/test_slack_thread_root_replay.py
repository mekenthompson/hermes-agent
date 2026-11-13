"""Regression: an old thread ROOT must never be re-admitted as a user turn.

Marko (2026-10): Slack re-emits ``message_changed`` for a thread parent whenever a reply lands.
After a restart the in-memory ``_processed_message_ts`` guard is empty, so the weeks-old root
became a second role=user turn beside Lisa's new reply and was answered again.
"""

import asyncio
import importlib
from unittest.mock import AsyncMock

from gateway.config import PlatformConfig

_slack_mod = importlib.import_module("plugins.platforms.slack.adapter")
SlackAdapter = _slack_mod.SlackAdapter

CHANNEL, TEAM, USER = "C0BRZNNLDJS", "T0BRVDZQ5D1", "U0BSW2AS8LQ"
ROOT_TS = "1791945028.574289"
REPLY_TS = "1792391525.282089"
ROOT_TEXT = "review this email and draft a recomendation email"


def _adapter(delivered):
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake"))
    adapter._bot_user_id = "U0BCLP7DB7B"
    adapter._resolve_user_name = AsyncMock(return_value="Lisa")
    adapter._fetch_thread_context = AsyncMock(return_value="[Thread context]\n" + ROOT_TEXT)

    async def _capture(event):
        delivered.append(event)

    adapter.handle_message = _capture
    return adapter


def _reply():
    return {"type": "message", "user": USER, "text": "<@U0BCLP7DB7B> can you draft an example",
            "channel": CHANNEL, "channel_type": "channel", "team": TEAM,
            "ts": REPLY_TS, "thread_ts": ROOT_TS}


def _parent_changed(**message_extra):
    message = {"type": "message", "user": USER, "text": ROOT_TEXT, "ts": ROOT_TS,
               "thread_ts": ROOT_TS, "reply_count": 4, "latest_reply": REPLY_TS}
    message.update(message_extra)
    return {"type": "message", "subtype": "message_changed", "channel": CHANNEL,
            "channel_type": "channel", "team": TEAM, "ts": "1792391526.900000",
            "event_ts": "1792391526.900000", "message": message,
            "previous_message": {"text": ROOT_TEXT, "ts": ROOT_TS}}


def _run(adapter, *events):
    async def scenario():
        for ev in events:
            await adapter._handle_slack_message(ev, {"team_id": TEAM})
    asyncio.run(scenario())


def test_parent_message_changed_with_empty_guard_is_not_a_turn():
    """Restart simulated: guard empty. New reply + parent bookkeeping change => one turn."""
    delivered = []
    adapter = _adapter(delivered)
    assert ROOT_TS not in adapter._processed_message_ts
    _run(adapter, _reply(), _parent_changed())
    assert [e.message_id for e in delivered] == [REPLY_TS]


def test_parent_change_arriving_before_reply_is_not_a_turn():
    delivered = []
    adapter = _adapter(delivered)
    _run(adapter, _parent_changed(), _reply())
    assert [e.message_id for e in delivered] == [REPLY_TS]


def test_parent_change_after_eviction_of_guard():
    delivered = []
    adapter = _adapter(delivered)
    adapter._processed_message_ts.clear()
    _run(adapter, _reply(), _parent_changed(edited={"user": USER, "ts": "1791945100.000000"}))
    assert [e.message_id for e in delivered] == [REPLY_TS]


def test_genuine_human_edit_of_root_is_still_admitted():
    delivered = []
    adapter = _adapter(delivered)
    ev = _parent_changed(text="<@U0BCLP7DB7B> " + ROOT_TEXT + " (edited)",
                         edited={"user": USER, "ts": "1792391526.800000"})
    ev["previous_message"]["text"] = ROOT_TEXT
    _run(adapter, ev)
    assert len(delivered) == 1
