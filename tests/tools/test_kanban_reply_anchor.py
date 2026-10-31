"""Native subscription capture keeps the verified source reply identity."""
import pytest

from gateway import session_context
from tools import kanban_tools


@pytest.mark.parametrize("message_id", ["activity", ""])
def test_linear_notify_target_preserves_source_activity_without_fallback(monkeypatch, message_id):
    values = {"HERMES_SESSION_PLATFORM": "linear", "HERMES_SESSION_CHAT_ID": "issue",
              "HERMES_SESSION_THREAD_ID": "session", "HERMES_SESSION_MESSAGE_ID": message_id,
              "HERMES_SESSION_PROFILE": "default"}
    monkeypatch.setattr(session_context, "get_session_env", lambda key, default="": values.get(key, default))
    target = kanban_tools._resolve_notify_target()
    assert target["thread_id"] == "session"
    assert target["delivery_metadata"].get("reply_to", "") == message_id
