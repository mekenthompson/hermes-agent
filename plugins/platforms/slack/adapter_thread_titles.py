"""Best-effort provisional and semantic titles for Slack assistant DM threads."""

import logging
import unicodedata

logger = logging.getLogger("plugins.platforms.slack.adapter")

# rename does not create a session or join this agent to one. Slack answers
# session_not_found when no session exists, and not_authorized when the thread
# has a session this agent has not joined yet (a new DM thread before the first
# status or stream). setStatus creates or joins the session; rename then works.
_JOIN_SESSION = frozenset({"session_not_found", "not_authorized"})

# Refusals that will not change by retrying the same thread: log one debug line
# and stop asking for that title kind, instead of a warning on every turn.
_UNTITLEABLE = frozenset({
    "access_denied", "channel_not_found", "enterprise_is_restricted", "feature_disabled",
    "invalid_arguments", "invalid_name", "is_archived", "method_deprecated", "missing_scope",
    "no_permission", "not_allowed_token_type", "not_authorized", "not_in_channel",
    "session_not_found", "thread_ts_not_allowed", "thread_ts_required",
})

# Slack validates session titles like channel names: at most 80 characters, no
# leading whitespace, no control or invisible characters, and none of these
# ASCII symbols. Other punctuation and non-ASCII text (accents, emoji) is fine.
_TITLE_MAX = 80
_TITLE_REJECTED = frozenset("#%/:@*+;<>\\~`$^[]{}")


def _session_title(source: str) -> str:
    """Return a title Slack accepts, or ``""`` when nothing usable is left."""
    out = []
    for ch in source:
        if ch == "%":
            out.append(" percent ")
        elif ch == "#":
            continue  # "PR #141" reads fine as "PR 141"
        elif ch in _TITLE_REJECTED or ch.isspace() or unicodedata.category(ch).startswith("C"):
            out.append(" ")
        else:
            out.append(ch)
    title = " ".join("".join(out).split())
    if len(title) > _TITLE_MAX:
        cut = title[:_TITLE_MAX - 1]
        head = cut.rsplit(" ", 1)[0]
        cut = head if len(head) >= _TITLE_MAX // 2 else cut
        title = cut.rstrip(" -_.,") + "\u2026"
    return title if any(ch.isalnum() for ch in title) else ""


def _log_title_failure(what: str, exc: BaseException) -> bool:
    """Log a title refusal. Returns True when retrying this thread is pointless."""
    code = _slack_error_code(exc)
    if code in _UNTITLEABLE:
        logger.debug("[Slack] %s skipped: %s", what, code)
        return True
    if code:
        logger.warning("[Slack] %s failed: %s", what, code)
    else:
        logger.warning("[Slack] %s failed: %s", what, type(exc).__name__, exc_info=True)
    return False


def _slack_error_code(exc: BaseException) -> str:
    """Structured Slack ``error`` field, including mapping-like SDK responses."""
    response = getattr(exc, "response", None)
    getter = getattr(response, "get", None)
    if not callable(getter):
        return ""
    try:
        code = getter("error")
    except (AttributeError, TypeError, KeyError):
        return ""
    return str(code or "")


def _response_title(response) -> str:
    """Title echoed by a Slack response, including mapping-like SDK bodies."""
    getter = getattr(response, "get", None)
    title = None
    if callable(getter):
        try:
            title = getter("title")
        except (AttributeError, TypeError, KeyError):
            title = None
    if isinstance(title, str):
        return title
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        nested = data.get("title")
        if isinstance(nested, str):
            return nested
    return ""


def _agent_sessions_enabled() -> bool:
    try:
        from plugins.platforms.slack.adapter import _sdk_supports_agent_sessions
    except ImportError:  # flat plugin-directory import
        from adapter import _sdk_supports_agent_sessions
    return bool(_sdk_supports_agent_sessions())


def _agent_session_lifecycle_status(status: str) -> str:
    """Map a legacy typing phrase onto ``agents.sessions.setStatus``.

    That method rejects free text and an empty clear. ``processing`` is the
    working state; ``active`` is the ready state that replaced an empty clear.
    """
    return "processing" if str(status or "").strip() else "active"


class AssistantThreadTitles:
    def _assistant_thread_title_enabled(self) -> bool:
        raw = self.config.extra.get("assistant_thread_titles", True)
        if isinstance(raw, str):
            return raw.strip().lower() not in {"0", "false", "no", "off"}
        return bool(raw)

    async def _write_session_title(self, client, channel_id: str, thread_ts: str, title: str) -> None:
        """Rename the session, creating it first when Slack has not opened one yet."""
        rename = self._session_title_method(client)
        try:
            await rename(channel_id=channel_id, thread_ts=thread_ts, title=title)
            return
        except Exception as exc:
            if not _agent_sessions_enabled() or _slack_error_code(exc) not in _JOIN_SESSION:
                raise
        # setStatus creates or joins the session and applies the title only on
        # create. A failed follow-up rename keeps the guard only when Slack echoed
        # that title.
        created = await self._session_status_method(client)(
            channel_id=channel_id, thread_ts=thread_ts, status="active", title=title)
        try:
            await rename(channel_id=channel_id, thread_ts=thread_ts, title=title)
        except Exception as exc:
            # setStatus applies title only on create. Keep the guard only when
            # Slack echoed that title; otherwise a later message must be able to retry.
            if _response_title(created) != title:
                raise
            _log_title_failure("session title follow-up rename", exc)

    async def _set_thread_status(
        self, chat_id: str, team_id: str, thread_ts: str, status: str, fail_label: str) -> None:
        """Set or clear the thread status. Agent Sessions uses the lifecycle enum."""
        if _agent_sessions_enabled():
            status = _agent_session_lifecycle_status(status)
        try:
            setter = self._session_status_method(self._get_client(chat_id, team_id=team_id))
            await setter(channel_id=chat_id, thread_ts=thread_ts, status=status)
        except Exception as exc:
            logger.warning(
                "[Slack] session status %s: %s",
                fail_label, _slack_error_code(exc) or type(exc).__name__,
                exc_info=True,
            )

    async def _set_assistant_thread_title(
        self, channel_id: str, thread_ts: str, title_source: str, *, team_id: str = "", semantic: bool = False) -> None:
        """Best-effort title for visible Slack AI DM threads."""
        if (
            not self._app or not channel_id or not thread_ts or not title_source
            or not self._assistant_thread_title_enabled()):
            return
        key = self._workspace_thread_key(team_id, channel_id, thread_ts)
        if not key or (
            key in self._titled_assistant_threads
            and (not semantic or key in self._semantically_titled_assistant_threads)
        ):
            return
        if title_source.lstrip().startswith("/"):
            return
        title = _session_title(title_source)
        if not title:
            return
        try:
            await self._write_session_title(
                self._get_client(channel_id, team_id=team_id), channel_id, thread_ts, title)
        except Exception as exc:  # health: allow BLE001 -- best-effort title; _log_title_failure keeps the traceback for non-Slack errors
            if not _log_title_failure("session title set", exc):
                return
            # Slack will refuse this thread again; stop asking on every turn.
        self._titled_assistant_threads.add(key)
        if semantic:
            self._semantically_titled_assistant_threads.add(key)
        # Evict oldest thread_ts first so recently titled threads keep their guard.
        self._evict_oldest_by_ts(
            self._titled_assistant_threads, self._TITLED_ASSISTANT_THREADS_MAX, lambda e: e[2])
        self._evict_oldest_by_ts(
            self._semantically_titled_assistant_threads, self._TITLED_ASSISTANT_THREADS_MAX, lambda e: e[2])
