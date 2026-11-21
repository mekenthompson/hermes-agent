"""The gateway's trusted source binding and the board author use one profile identity."""

from types import SimpleNamespace

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource
from tools.kanban_tools import _persisted_identity, _resolve_notify_target


def test_isolated_default_home_binds_origin_to_native_author(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="human",
                           thread_id="topic", profile="default")
    context = SessionContext(source=source, connected_platforms=[], home_channels={},
                             session_key="agent:main:telegram:chat", session_id="conversation")
    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True), adapters={})
    tokens = GatewayRunner._set_session_env(runner, context)
    try:
        target = _resolve_notify_target()
        assert target["notifier_profile"] == _persisted_identity() == "default"
        assert (target["platform"], target["chat_id"], target["thread_id"]) == ("telegram", "chat", "topic")
    finally:
        GatewayRunner._clear_session_env(runner, tokens)
