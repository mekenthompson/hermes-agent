import asyncio
import os

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter, _apply_yaml_config


def run(coro):
    return asyncio.run(coro)


def make_adapter(extra=None):
    config = PlatformConfig(extra=extra or {})
    adapter = SlackAdapter(config)
    adapter._bot_user_id = "UBOT"
    adapter._team_bot_user_ids["T1"] = "UBOT"
    adapter._has_active_session_for_thread = lambda **_: False

    async def no_thread_context(**_):
        return ""

    async def no_parent_text(**_):
        return ""

    async def user_name(*_, **__):
        return "Sebastian"

    adapter._fetch_thread_context = no_thread_context
    adapter._fetch_thread_parent_text = no_parent_text
    adapter._resolve_user_name = user_name
    return adapter


def slack_event(text, ts="100.000", thread_ts=None):
    event = {
        "type": "message",
        "channel": "C123",
        "channel_type": "channel",
        "team": "T1",
        "user": "U123",
        "text": text,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return event


def test_thread_require_mention_env_bridge(monkeypatch):
    monkeypatch.delenv("SLACK_THREAD_REQUIRE_MENTION", raising=False)

    _apply_yaml_config(
        {},
        {
            "thread_require_mention": True,
        },
    )

    assert os.environ["SLACK_THREAD_REQUIRE_MENTION"] == "true"


def test_thread_participation_channels_yaml_bridge(monkeypatch):
    monkeypatch.delenv("SLACK_THREAD_PARTICIPATION_CHANNELS", raising=False)

    _apply_yaml_config({}, {"thread_participation_channels": ["C123", "C456"]})

    assert os.environ["SLACK_THREAD_PARTICIPATION_CHANNELS"] == "C123,C456"
    os.environ.pop("SLACK_THREAD_PARTICIPATION_CHANNELS", None)


def test_thread_require_mention_parses_yaml_and_env(monkeypatch):
    monkeypatch.setenv("SLACK_THREAD_REQUIRE_MENTION", "true")

    assert make_adapter()._slack_thread_require_mention() is True
    assert (
        make_adapter({"thread_require_mention": "false"})._slack_thread_require_mention()
        is False
    )
    assert make_adapter({"thread_require_mention": True})._slack_thread_require_mention() is True


def test_thread_require_mention_allows_top_level_free_response():
    adapter = make_adapter(
        {
            "allowed_channels": ["C123"],
            "require_mention": False,
            "thread_require_mention": True,
            "reply_in_thread": True,
        }
    )
    handled = []

    async def capture(event):
        handled.append(event)

    adapter.handle_message = capture

    run(adapter._handle_slack_message(slack_event("vpn is broken", ts="100.000")))

    assert len(handled) == 1
    assert handled[0].text == "vpn is broken"
    assert handled[0].source.thread_id == "100.000"


def test_thread_require_mention_blocks_unmentioned_thread_reply():
    adapter = make_adapter(
        {
            "allowed_channels": ["C123"],
            "require_mention": False,
            "thread_require_mention": True,
            "reply_in_thread": True,
        }
    )
    handled = []

    async def capture(event):
        handled.append(event)

    adapter.handle_message = capture

    run(
        adapter._handle_slack_message(
            slack_event("we found another 403", ts="101.000", thread_ts="100.000")
        )
    )

    assert handled == []


def test_thread_require_mention_allows_mentioned_thread_reply_without_sticky_thread():
    adapter = make_adapter(
        {
            "allowed_channels": ["C123"],
            "require_mention": False,
            "thread_require_mention": True,
            "reply_in_thread": True,
        }
    )
    handled = []

    async def capture(event):
        handled.append(event)

    adapter.handle_message = capture

    run(
        adapter._handle_slack_message(
            slack_event("<@UBOT> update this", ts="101.000", thread_ts="100.000")
        )
    )

    assert len(handled) == 1
    assert handled[0].text == "update this"
    assert "100.000" not in adapter._mentioned_threads

    run(
        adapter._handle_slack_message(
            slack_event("follow-up without mention", ts="102.000", thread_ts="100.000")
        )
    )

    assert len(handled) == 1


@pytest.mark.parametrize("strict_mention,thread_require_mention", [
    (False, False), (False, True), (True, False), (True, True),
])
def test_participation_channel_follows_invited_thread_for_every_legacy_flag_combination(
    strict_mention, thread_require_mention,
):
    """The channel opt-in takes precedence only for a thread this bot joined."""
    adapter = make_adapter(
        {
            "allowed_channels": ["C123"],
            "require_mention": False,
            "strict_mention": strict_mention,
            "thread_require_mention": thread_require_mention,
            "thread_participation_channels": ["C123"],
        }
    )
    handled = []

    async def capture(event):
        handled.append(event)

    adapter.handle_message = capture
    run(adapter._handle_slack_message(
        slack_event("<@UBOT> investigate this", ts="101.000", thread_ts="100.000")
    ))
    run(adapter._handle_slack_message(
        slack_event("here is the requested log", ts="102.000", thread_ts="100.000")
    ))

    assert [event.text for event in handled] == [
        "investigate this",
        "here is the requested log",
    ]


@pytest.mark.parametrize(("strict_mention", "thread_require_mention", "follows"), [
    (False, False, True), (False, True, False), (True, False, False), (True, True, False),
])
def test_without_participation_optin_preserves_legacy_thread_gating(
    strict_mention, thread_require_mention, follows,
):
    """No selected channel means strict/thread flags retain their existing behavior."""
    adapter = make_adapter(
        {
            "allowed_channels": ["C123"],
            "require_mention": True,
            "strict_mention": strict_mention,
            "thread_require_mention": thread_require_mention,
        }
    )
    handled = []

    async def capture(event):
        handled.append(event)

    adapter.handle_message = capture
    run(adapter._handle_slack_message(
        slack_event("<@UBOT> investigate this", ts="101.000", thread_ts="100.000")
    ))
    run(adapter._handle_slack_message(
        slack_event("here is the requested log", ts="102.000", thread_ts="100.000")
    ))

    assert [event.text for event in handled] == (
        ["investigate this", "here is the requested log"]
        if follows else ["investigate this"]
    )


def test_participation_optin_does_not_relax_top_level_strict_mention():
    """The exception is scoped to follow-ups, not the selected channel's primary."""
    adapter = make_adapter(
        {
            "allowed_channels": ["C123"],
            "require_mention": True,
            "strict_mention": True,
            "thread_require_mention": True,
            "thread_participation_channels": ["C123"],
        }
    )
    handled = []

    async def capture(event):
        handled.append(event)

    adapter.handle_message = capture
    run(adapter._handle_slack_message(slack_event("unmentioned top-level request", ts="101.000")))

    assert handled == []


def test_non_selected_channel_preserves_legacy_thread_gating():
    """Listing another channel cannot relax strict/thread gating in this one."""
    adapter = make_adapter(
        {
            "allowed_channels": ["C123"],
            "require_mention": False,
            "strict_mention": True,
            "thread_require_mention": True,
            "thread_participation_channels": ["C456"],
        }
    )
    handled = []

    async def capture(event):
        handled.append(event)

    adapter.handle_message = capture
    run(adapter._handle_slack_message(
        slack_event("<@UBOT> investigate this", ts="101.000", thread_ts="100.000")
    ))
    run(adapter._handle_slack_message(
        slack_event("here is the requested log", ts="102.000", thread_ts="100.000")
    ))

    assert [event.text for event in handled] == ["investigate this"]


def test_participation_only_thread_channel_blocks_uninvited_thread():
    """The primary does not barge into a secondary agent's thread."""
    adapter = make_adapter(
        {
            "allowed_channels": ["C123"],
            "require_mention": False,
            "strict_mention": True,
            "thread_require_mention": True,
            "thread_participation_channels": ["C123"],
        }
    )
    handled = []

    async def capture(event):
        handled.append(event)

    adapter.handle_message = capture
    run(adapter._handle_slack_message(
        slack_event("secondary agent's follow-up", ts="101.000", thread_ts="100.000")
    ))

    assert handled == []


def test_participation_only_thread_channel_recovers_active_session_after_restart():
    """A scoped thread retains its existing durable session participation."""
    adapter = make_adapter(
        {
            "allowed_channels": ["C123"],
            "require_mention": False,
            "strict_mention": True,
            "thread_require_mention": True,
            "thread_participation_channels": ["C123"],
        }
    )
    def active_session(channel_id, thread_ts, user_id, team_id="", *, chat_type="group"):
        return True

    adapter._has_active_session_for_thread = active_session
    handled = []

    async def capture(event):
        handled.append(event)

    adapter.handle_message = capture
    run(adapter._handle_slack_message(
        slack_event("resume from the last result", ts="101.000", thread_ts="100.000")
    ))

    assert [event.text for event in handled] == ["resume from the last result"]


def test_participation_thread_parent_mention_is_scoped_to_its_workspace():
    """A recovered parent mention cannot enroll an equal timestamp in another workspace."""
    adapter = make_adapter(
        {
            "allowed_channels": ["C123"],
            "require_mention": False,
            "strict_mention": True,
            "thread_require_mention": True,
            "thread_participation_channels": ["C123"],
        }
    )

    async def parent_text(channel_id, thread_ts, team_id="", strip_bot_mention=True):
        return "<@UBOT> investigate" if team_id == "T1" else ""

    adapter._fetch_thread_parent_text = parent_text
    handled = []

    async def capture(event):
        handled.append(event)

    adapter.handle_message = capture
    run(adapter._handle_slack_message(
        slack_event("first workspace follow-up", ts="101.000", thread_ts="100.000")
    ))
    other_workspace_event = slack_event(
        "other workspace follow-up", ts="102.000", thread_ts="100.000")
    other_workspace_event["team"] = "T2"
    run(adapter._handle_slack_message(other_workspace_event))

    assert [event.text for event in handled] == ["first workspace follow-up"]
    assert ("T1", "100.000") in adapter._mentioned_threads
    assert "100.000" not in adapter._mentioned_threads
