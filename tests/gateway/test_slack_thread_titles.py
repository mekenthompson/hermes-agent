"""Slack assistant DM thread titles: generated titles, session join, Slack-valid text.

Slack's ``agents.sessions.rename`` neither creates a session nor joins the agent to one
(``session_not_found`` / ``not_authorized`` on a fresh DM thread) and validates titles like channel
names (80 chars, no control characters, none of ``#%/:@*+;<>\\~`$^[]{}``). The model-generated title
arrives later than the first message, so the adapter must accept a second ("semantic") title.
"""

import asyncio
import types
import weakref
from unittest.mock import AsyncMock

import pytest

import plugins.platforms.slack.adapter as slack_mod
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tests.gateway.test_slack_agent_sessions_api import (
    TestAgentSessionsApiRouting as _AgentSessionsHelpers,
)


@pytest.fixture(autouse=True)
def _restore_sdk_probe():
    prev = slack_mod._AGENT_SESSIONS_SUPPORTED
    yield
    slack_mod._AGENT_SESSIONS_SUPPORTED = prev


class _SlackApiError(Exception):
    """``slack_sdk.SlackApiError`` shape: the adapter reads ``response["error"]``."""

    def __init__(self, error: str):
        super().__init__(error)
        self.response = {"error": error}


class _MappingResponse:
    """Mapping-like Slack body that is not a ``dict`` (``AsyncSlackResponse``)."""

    def __init__(self, **fields):
        self._fields = fields

    def get(self, key, default=None):
        return self._fields.get(key, default)


class _MappingSlackError(Exception):
    def __init__(self, error: str):
        super().__init__(error)
        self.response = _MappingResponse(error=error)


def _adapter():
    a = _AgentSessionsHelpers._adapter(None)
    a.config.extra["assistant_thread_titles"] = True
    return a


class TestSemanticTitles:
    @pytest.mark.asyncio
    async def test_generated_title_replaces_the_raw_one_once_and_failed_call_retries(self):
        slack_mod._AGENT_SESSIONS_SUPPORTED = False
        a = _adapter()
        rename = a._app.client.assistant_threads_setTitle = AsyncMock(
            side_effect=[None, RuntimeError("temporary"), None, None])
        await a._set_assistant_thread_title("D123", "171234.0001", "Raw words", team_id="T1")

        await a._set_assistant_thread_title("D123", "171234.0001", "Generated", team_id="T1", semantic=True)
        await a._set_assistant_thread_title("D123", "171234.0001", "Retry title", team_id="T1", semantic=True)
        await a._set_assistant_thread_title("D123", "171234.0001", "Later duplicate", team_id="T1", semantic=True)

        assert [c.kwargs["title"] for c in rename.await_args_list] == [
            "Raw words", "Generated", "Retry title"]

    @pytest.mark.asyncio
    async def test_guard_is_opt_out_and_workspace_scoped(self):
        slack_mod._AGENT_SESSIONS_SUPPORTED = False
        a = _adapter()
        a.config.extra["assistant_thread_titles"] = False
        rename = a._app.client.assistant_threads_setTitle = AsyncMock()
        await a._set_assistant_thread_title("D123", "171234.0001", "No title", team_id="T1", semantic=True)
        assert rename.await_count == 0

        a.config.extra["assistant_thread_titles"] = True
        await a._set_assistant_thread_title("D123", "171234.0001", "T1 title", team_id="T1", semantic=True)
        await a._set_assistant_thread_title("D123", "171234.0001", "T2 title", team_id="T2", semantic=True)
        assert [c.kwargs["title"] for c in rename.await_args_list] == ["T1 title", "T2 title"]

    @pytest.mark.asyncio
    async def test_guard_state_stays_bounded(self):
        slack_mod._AGENT_SESSIONS_SUPPORTED = False
        a = _adapter()
        a._app.client.assistant_threads_setTitle = AsyncMock()
        cap = a._TITLED_ASSISTANT_THREADS_MAX
        for i in range(cap + 25):
            await a._set_assistant_thread_title(
                "D123", f"{1700000000 + i}.000100", f"Title {i}", team_id="T1", semantic=True)
        assert len(a._titled_assistant_threads) <= cap
        assert len(a._semantically_titled_assistant_threads) <= cap


class TestJoinSession:
    @pytest.mark.asyncio
    async def test_missing_session_is_created_then_keeps_the_generated_title(self):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = _adapter()
        rename = a._app.client.agents_sessions_rename = AsyncMock(
            side_effect=[_SlackApiError("session_not_found"), None])
        create = a._app.client.agents_sessions_setStatus = AsyncMock()

        await a._set_assistant_thread_title(
            "D123", "171234.0001", "Generated title", team_id="T1", semantic=True)

        assert create.await_args.kwargs == {
            "channel_id": "D123", "thread_ts": "171234.0001", "status": "active", "title": "Generated title"}
        assert [c.kwargs["title"] for c in rename.await_args_list] == ["Generated title", "Generated title"]
        await a._set_assistant_thread_title("D123", "171234.0001", "later raw message", team_id="T1")
        assert rename.await_count == 2

    @pytest.mark.asyncio
    async def test_not_authorized_joins_the_session_then_renames(self):
        # A new DM thread answers not_authorized until this agent has set a status on it.
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = _adapter()
        rename = a._app.client.agents_sessions_rename = AsyncMock(
            side_effect=[_MappingSlackError("not_authorized"), None])
        join = a._app.client.agents_sessions_setStatus = AsyncMock()

        await a._set_assistant_thread_title("D123", "171234.0001", "First prompt", team_id="T1")

        assert join.await_args.kwargs["status"] == "active"
        assert join.await_args.kwargs["title"] == "First prompt"
        assert rename.await_count == 2
        assert ("T1", "D123", "171234.0001") in a._titled_assistant_threads

    @pytest.mark.asyncio
    async def test_created_title_survives_a_failed_follow_up_rename(self):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = _adapter()
        rename = a._app.client.agents_sessions_rename = AsyncMock(
            side_effect=[_SlackApiError("session_not_found"), _SlackApiError("ratelimited")])
        a._app.client.agents_sessions_setStatus = AsyncMock(
            return_value=_MappingResponse(ok=True, title="Generated title"))

        await a._set_assistant_thread_title(
            "D123", "171234.0001", "Generated title", team_id="T1", semantic=True)
        await a._set_assistant_thread_title("D123", "171234.0001", "later raw message", team_id="T1")

        assert rename.await_count == 2
        assert ("T1", "D123", "171234.0001") in a._semantically_titled_assistant_threads

    @pytest.mark.asyncio
    async def test_unconfirmed_create_does_not_block_a_later_title(self):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = _adapter()
        rename = a._app.client.agents_sessions_rename = AsyncMock(
            side_effect=[_SlackApiError("session_not_found"), _SlackApiError("ratelimited"), None])
        a._app.client.agents_sessions_setStatus = AsyncMock(
            return_value=_MappingResponse(ok=True, title="different"))

        await a._set_assistant_thread_title(
            "D123", "171234.0001", "Generated title", team_id="T1", semantic=True)
        await a._set_assistant_thread_title(
            "D123", "171234.0001", "Generated title", team_id="T1", semantic=True)

        assert rename.await_count == 3
        assert ("T1", "D123", "171234.0001") in a._semantically_titled_assistant_threads

    @pytest.mark.asyncio
    async def test_persistent_refusal_is_one_quiet_line_and_not_retried_each_turn(self, caplog):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = _adapter()
        rename = a._app.client.agents_sessions_rename = AsyncMock(side_effect=_SlackApiError("not_authorized"))
        a._app.client.agents_sessions_setStatus = AsyncMock(return_value=_MappingResponse(ok=True))

        with caplog.at_level("DEBUG", logger="plugins.platforms.slack.adapter"):
            await a._set_assistant_thread_title("D123", "171234.0001", "First prompt", team_id="T1")
            await a._set_assistant_thread_title("D123", "171234.0001", "Second prompt", team_id="T1")

        assert rename.await_count == 2  # first rename + post-join rename; second turn skipped
        records = [r for r in caplog.records if "session title" in r.getMessage()]
        assert [r.levelname for r in records] == ["DEBUG"]

    @pytest.mark.asyncio
    async def test_unrelated_error_is_warned_and_does_not_create(self, caplog):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = _adapter()
        a._app.client.agents_sessions_rename = AsyncMock(side_effect=_SlackApiError("invalid_auth"))
        create = a._app.client.agents_sessions_setStatus = AsyncMock()

        with caplog.at_level("WARNING", logger="plugins.platforms.slack.adapter"):
            await a._set_assistant_thread_title(
                "D123", "171234.0001", "Nope", team_id="T1", semantic=True)

        assert create.await_count == 0
        assert not a._semantically_titled_assistant_threads
        assert any("invalid_auth" in r.getMessage() for r in caplog.records)


class TestSlackValidTitle:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("source, expected", [
        ("Calculate 15% of 240", "Calculate 15 percent of 240"),
        ("Research cheapest 2 TB cloud backups #2", "Research cheapest 2 TB cloud backups 2"),
        ("fix: a/b @here *now* [x] {y} ~z `q` $1 ^2 +3; <4>", "fix a b here now x y z q 1 2 3 4"),
        ("  leading\nnewline\ttab\u00a0nbsp\u200bzero-width", "leading newline tab nbsp zero-width"),
        ("It's (draft) = A & B | C, done! \u2014 \u00e9t\u00e9 \u2705?", "It's (draft) = A & B | C, done! \u2014 \u00e9t\u00e9 \u2705?"),
    ])
    async def test_title_is_cleaned_to_what_slack_accepts(self, source, expected):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = _adapter()
        rename = a._app.client.agents_sessions_rename = AsyncMock()

        await a._set_assistant_thread_title("D123", "171234.0001", source, team_id="T1")

        assert rename.await_args.kwargs["title"] == expected

    @pytest.mark.asyncio
    async def test_long_title_fits_the_limit_on_a_word_boundary(self):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = _adapter()
        rename = a._app.client.agents_sessions_rename = AsyncMock()

        await a._set_assistant_thread_title("D123", "171234.0001", "word " * 40, team_id="T1")

        title = rename.await_args.kwargs["title"]
        assert len(title) <= 80
        assert title.endswith("word\u2026")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["#/:@", "...", "   ", "/help me"])
    async def test_untitleable_or_command_text_makes_no_call(self, source):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = _adapter()
        rename = a._app.client.agents_sessions_rename = AsyncMock()

        await a._set_assistant_thread_title("D123", "171234.0001", source, team_id="T1")

        rename.assert_not_called()


class TestRunnerLane:
    @pytest.mark.asyncio
    async def test_generated_title_reaches_the_receiving_adapter_with_its_workspace(self):
        calls = []
        delivered = asyncio.Event()

        class Adapter:
            async def _set_assistant_thread_title(self, channel, thread, title, *, team_id, semantic):
                calls.append((channel, thread, title, team_id, semantic))
                delivered.set()

        receiving, other = Adapter(), Adapter()
        runner = object.__new__(GatewayRunner)
        runner.config = types.SimpleNamespace(multiplex_profiles=True)
        runner.adapters = {Platform.SLACK: receiving}
        runner._profile_adapters = {"routed": {Platform.SLACK: other}}
        source = SessionSource(platform=Platform.SLACK, chat_id="D123", chat_type="dm",
                               thread_id="171234.0001", scope_id="T1", profile="routed")
        source._transport_adapter_ref = weakref.ref(receiving)

        runner._schedule_slack_assistant_thread_title(source, "session-1", "Fleet thread title settings")
        await asyncio.wait_for(delivered.wait(), timeout=2)

        assert calls == [("D123", "171234.0001", "Fleet thread title settings", "T1", True)]

    @pytest.mark.parametrize("platform,chat_type,thread_id", [
        (Platform.SLACK, "group", "171234.0001"),
        (Platform.SLACK, "dm", None),
        (Platform.TELEGRAM, "dm", "171234.0001"),
    ])
    def test_other_surfaces_are_not_renamed(self, platform, chat_type, thread_id):
        scheduled = []
        runner = object.__new__(GatewayRunner)
        runner._schedule_rename_from_title_thread = lambda *args: scheduled.append(args)
        source = SessionSource(platform=platform, chat_id="chat", chat_type=chat_type, thread_id=thread_id)
        runner._schedule_slack_assistant_thread_title(source, "session-1", "Semantic title")
        assert scheduled == []
