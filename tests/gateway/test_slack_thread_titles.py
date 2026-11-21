"""Slack semantic thread-title state transitions."""

from unittest.mock import AsyncMock

import pytest

import plugins.platforms.slack.adapter as slack_mod
from tests.gateway.test_slack import _pin_legacy_assistant_threads_api
from tests.gateway.test_slack_agent_sessions_api import (
    TestAgentSessionsApiRouting as _AgentSessionsHelpers,
)


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


class TestSemanticThreadTitles:
    _adapter = _AgentSessionsHelpers._adapter

    @pytest.mark.asyncio
    async def test_semantic_title_replaces_raw_once_and_failed_call_retries(self):
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        rename = a._app.client.assistant_threads_setTitle = AsyncMock(
            side_effect=[None, RuntimeError("temporary"), None, None]
        )
        await a._set_assistant_thread_title("D123", "171234.0001", "Raw words", team_id="T1")
        assert a._titled_assistant_threads

        await a._set_assistant_thread_title("D123", "171234.0001", "Semantic title", team_id="T1", semantic=True)
        await a._set_assistant_thread_title("D123", "171234.0001", "Retry title", team_id="T1", semantic=True)
        assert rename.await_count == 3
        assert rename.await_args_list[1].kwargs["title"] == "Semantic title"
        assert rename.await_args_list[2].kwargs["title"] == "Retry title"
        assert len(a._semantically_titled_assistant_threads) == 1
        await a._set_assistant_thread_title("D123", "171234.0001", "Later duplicate", team_id="T1", semantic=True)
        assert rename.await_count == 3

    @pytest.mark.asyncio
    async def test_semantic_title_guard_is_opt_out_and_workspace_scoped(self):
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = False
        a._app.client.assistant_threads_setTitle = AsyncMock()
        await a._set_assistant_thread_title("D123", "171234.0001", "No title", team_id="T1")
        assert a._app.client.assistant_threads_setTitle.await_count == 0
        a.config.extra["assistant_thread_titles"] = True
        rename = a._app.client.assistant_threads_setTitle
        await a._set_assistant_thread_title("D123", "171234.0001", "T1 title", team_id="T1", semantic=True)
        await a._set_assistant_thread_title("D123", "171234.0001", "T2 title", team_id="T2", semantic=True)
        assert [call.kwargs["title"] for call in rename.await_args_list] == ["T1 title", "T2 title"]

    @pytest.mark.asyncio
    async def test_missing_agent_session_is_created_then_keeps_the_generated_title(self):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        rename = a._app.client.agents_sessions_rename = AsyncMock(
            side_effect=[_SlackApiError("session_not_found"), None]
        )
        create = a._app.client.agents_sessions_setStatus = AsyncMock()
        a._app.client.assistant_threads_setTitle = AsyncMock()

        await a._set_assistant_thread_title(
            "D123", "171234.0001", "Generated title", team_id="T1", semantic=True)

        assert create.await_args.kwargs == {
            "channel_id": "D123",
            "thread_ts": "171234.0001",
            "status": "active",
            "title": "Generated title",
        }
        assert [call.kwargs["title"] for call in rename.await_args_list] == [
            "Generated title",
            "Generated title",
        ]
        await a._set_assistant_thread_title("D123", "171234.0001", "later raw message", team_id="T1")
        assert rename.await_count == 2
        a._app.client.assistant_threads_setTitle.assert_not_called()

    @pytest.mark.asyncio
    async def test_created_title_survives_a_failed_follow_up_rename(self):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        rename = a._app.client.agents_sessions_rename = AsyncMock(
            side_effect=[_SlackApiError("session_not_found"), _SlackApiError("ratelimited")]
        )
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
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        rename = a._app.client.agents_sessions_rename = AsyncMock(
            side_effect=[
                _SlackApiError("session_not_found"),
                _SlackApiError("ratelimited"),
                None,
            ])
        a._app.client.agents_sessions_setStatus = AsyncMock(
            return_value=_MappingResponse(ok=True, title="different"))

        await a._set_assistant_thread_title(
            "D123", "171234.0001", "Generated title", team_id="T1", semantic=True)
        await a._set_assistant_thread_title(
            "D123", "171234.0001", "Generated title", team_id="T1", semantic=True)

        assert rename.await_count == 3
        assert ("T1", "D123", "171234.0001") in a._semantically_titled_assistant_threads

    @pytest.mark.asyncio
    async def test_mapping_like_session_not_found_creates_the_session(self):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        a._app.client.agents_sessions_rename = AsyncMock(
            side_effect=[_MappingSlackError("session_not_found"), None])
        create = a._app.client.agents_sessions_setStatus = AsyncMock()

        await a._set_assistant_thread_title(
            "D123", "171234.0001", "Generated title", team_id="T1", semantic=True)

        assert create.await_count == 1

    @pytest.mark.asyncio
    async def test_agent_session_typing_uses_the_status_enum(self):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        status = a._app.client.agents_sessions_setStatus = AsyncMock()
        a._app.client.assistant_threads_setStatus = AsyncMock()

        await a.send_typing("D123", metadata={"thread_id": "171234.0001"})
        await a.stop_typing("D123", metadata={"thread_id": "171234.0001"})

        assert [call.kwargs["status"] for call in status.await_args_list] == ["processing", "active"]
        a._app.client.assistant_threads_setStatus.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_authorized_joins_the_session_then_renames(self):
        # Live Slack (HF-656): a new DM thread answers not_authorized, not
        # session_not_found, until this agent has set a status on it.
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        rename = a._app.client.agents_sessions_rename = AsyncMock(
            side_effect=[_MappingSlackError("not_authorized"), None])
        join = a._app.client.agents_sessions_setStatus = AsyncMock()

        await a._set_assistant_thread_title("D123", "171234.0001", "First prompt", team_id="T1")

        assert join.await_args.kwargs == {
            "channel_id": "D123", "thread_ts": "171234.0001", "status": "active", "title": "First prompt"}
        assert rename.await_count == 2
        assert ("T1", "D123", "171234.0001") in a._titled_assistant_threads

    @pytest.mark.asyncio
    async def test_persistent_refusal_is_one_quiet_line_and_not_retried_each_turn(self, caplog):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        rename = a._app.client.agents_sessions_rename = AsyncMock(
            side_effect=_SlackApiError("not_authorized"))
        # Session already existed, so Slack ignored the create-only title.
        a._app.client.agents_sessions_setStatus = AsyncMock(return_value=_MappingResponse(ok=True))

        with caplog.at_level("DEBUG", logger="plugins.platforms.slack.adapter"):
            await a._set_assistant_thread_title("D123", "171234.0001", "First prompt", team_id="T1")
            await a._set_assistant_thread_title("D123", "171234.0001", "Second prompt", team_id="T1")

        assert rename.await_count == 2  # first rename + post-join rename; second turn skipped
        records = [r for r in caplog.records if "session title" in r.getMessage()]
        assert [r.levelname for r in records] == ["DEBUG"]
        assert records[0].exc_info is None
        assert "not_authorized" in records[0].getMessage()

    @pytest.mark.asyncio
    async def test_invalid_name_is_quiet_and_semantic_title_may_still_follow(self, caplog):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        rename = a._app.client.agents_sessions_rename = AsyncMock(
            side_effect=[_SlackApiError("invalid_name"), None])

        with caplog.at_level("DEBUG", logger="plugins.platforms.slack.adapter"):
            await a._set_assistant_thread_title("D123", "171234.0001", "raw", team_id="T1")
            await a._set_assistant_thread_title("D123", "171234.0001", "Semantic", team_id="T1", semantic=True)

        assert rename.await_count == 2
        assert not [r for r in caplog.records if r.levelname == "WARNING"]
        assert ("T1", "D123", "171234.0001") in a._semantically_titled_assistant_threads

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source, expected", [
        # Rejected live by agents.sessions.rename with invalid_name (HF-656).
        ("Calculate 15% of 240", "Calculate 15 percent of 240"),
        ("Research cheapest 2 TB cloud backups #2", "Research cheapest 2 TB cloud backups 2"),
        ("HF-656: fix a/b @here *now* [x] {y} ~z `q` $1 ^2 +3; <4>",
         "HF-656 fix a b here now x y z q 1 2 3 4"),
        ("  leading\nnewline\ttab\u00a0nbsp\u200bzero-width", "leading newline tab nbsp zero-width"),
        # Accepted live: kept as written.
        ("Ken's (draft) = A & B | C, done! \u2014 \u00e9t\u00e9 \u2705?", "Ken's (draft) = A & B | C, done! \u2014 \u00e9t\u00e9 \u2705?"),
    ])
    async def test_title_is_cleaned_to_what_slack_accepts(self, source, expected):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        rename = a._app.client.agents_sessions_rename = AsyncMock()

        await a._set_assistant_thread_title("D123", "171234.0001", source, team_id="T1")

        assert rename.await_args.kwargs["title"] == expected

    @pytest.mark.asyncio
    async def test_long_title_fits_slack_limit_on_a_word_boundary(self):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        rename = a._app.client.agents_sessions_rename = AsyncMock()

        await a._set_assistant_thread_title("D123", "171234.0001", "word " * 40, team_id="T1")

        title = rename.await_args.kwargs["title"]
        assert len(title) <= 80
        assert title.endswith("word\u2026")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["#/:@", "...", "   ", "/help me"])
    async def test_untitleable_or_command_text_makes_no_call(self, source):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        rename = a._app.client.agents_sessions_rename = AsyncMock()

        await a._set_assistant_thread_title("D123", "171234.0001", source, team_id="T1")

        rename.assert_not_called()

    @pytest.mark.asyncio
    async def test_unrelated_title_error_is_warned_and_does_not_create(self, caplog):
        slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        a._app.client.agents_sessions_rename = AsyncMock(side_effect=_SlackApiError("invalid_auth"))
        create = a._app.client.agents_sessions_setStatus = AsyncMock()

        with caplog.at_level("WARNING", logger="plugins.platforms.slack.adapter"):
            await a._set_assistant_thread_title(
                "D123", "171234.0001", "Nope", team_id="T1", semantic=True)

        assert create.await_count == 0
        assert not a._semantically_titled_assistant_threads
        assert any("invalid_auth" in record.message for record in caplog.records)
