"""Desktop approval must settle through the real server-request path.

A queued callback that calls the resolver directly is not that path. The
fixture user is a transport that returns a response frame, or an
``approval.respond`` RPC. Neither injects consent into the waiter.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from tools import approval_context
from tools.interrupt import set_interrupt


SESSION = "desktop-approval-loop"
UI = "desktop-ui"


@pytest.fixture()
def approval_env(monkeypatch):
    from tools import approval as mod

    saved = {
        "HERMES_SESSION_KEY": os.environ.get("HERMES_SESSION_KEY"),
        "HERMES_EXEC_ASK": os.environ.get("HERMES_EXEC_ASK"),
        "HERMES_INTERACTIVE": os.environ.get("HERMES_INTERACTIVE"),
        "HERMES_CRON_SESSION": os.environ.get("HERMES_CRON_SESSION"),
        "HERMES_SESSION_PLATFORM": os.environ.get("HERMES_SESSION_PLATFORM"),
    }
    monkeypatch.setenv("HERMES_SESSION_KEY", SESSION)
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "desktop")
    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()
    mod._session_approved.clear()
    yield mod
    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()
    set_interrupt(False)
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _manual(monkeypatch, seconds):
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"mode": "manual", "timeout": seconds})
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")


class _Peer:
    """Harmless stand-in for the Desktop socket. Records frames; does not consent."""

    def __init__(self, *, deliver: bool = True):
        self.frames: list[dict] = []
        self.wrote = threading.Event()
        self.deliver = deliver

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        self.wrote.set()
        return self.deliver

    def close(self) -> None:
        return None


def _bind_desktop_session(peer: _Peer):
    from tui_gateway import server

    server._sessions[UI] = {
        "session_key": SESSION,
        "transport": peer,
        "history": [],
        "history_lock": threading.Lock(),
    }
    return server


def _unbind(server) -> None:
    server._sessions.pop(UI, None)
    server._sessions.pop("other-ui", None)
    from tui_gateway import server_requests

    with server_requests._lock:
        stale = [rid for rid, req in server_requests._open.items() if req.sid in {UI, "sid-dead"}]
        for rid in stale:
            server_requests._open.pop(rid, None)


def _approval_frames(peer: _Peer) -> list[dict]:
    return [frame for frame in peer.frames if frame.get("method") == "approval"]


def _respond_with_frame(peer: _Peer, choice: str) -> threading.Thread:
    from tui_gateway import server_requests

    def run() -> None:
        assert peer.wrote.wait(timeout=5), "approval frame was never written"
        frame = _approval_frames(peer)[-1]
        server_requests.resolve_response({"id": frame["id"], "result": {"choice": choice}})

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _respond_with_rpc(server, peer: _Peer, choice: str) -> threading.Thread:
    def run() -> None:
        assert peer.wrote.wait(timeout=5), "approval frame was never written"
        frame = _approval_frames(peer)[-1]
        server.handle_request({
            "id": "approval-response",
            "method": "approval.respond",
            "params": {
                "session_id": UI,
                "request_id": frame["params"]["request_id"],
                "choice": choice,
            },
        })

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _wait_on_loop(mod, command: str):
    async def on_loop():
        return mod.check_all_command_guards(command, "local")

    return asyncio.run(on_loop())


def test_approve_once_response_frame_settles_without_persisting(approval_env, monkeypatch):
    """Waiter is on a running loop. The choice arrives as a response frame, not a resolver lambda."""
    mod = approval_env
    _manual(monkeypatch, 5)
    peer = _Peer()
    server = _bind_desktop_session(peer)
    mod.register_gateway_notify(SESSION, lambda data: server._emit_approval_request(UI, data))
    responder = _respond_with_frame(peer, "once")
    try:
        result = _wait_on_loop(mod, "rm -rf /var/cache/probe")
    finally:
        responder.join(timeout=5)
        mod.unregister_gateway_notify(SESSION)
        _unbind(server)
    assert result["approved"] is True
    assert result.get("user_approved") is True
    assert not mod._session_approved.get(SESSION)
    assert _approval_frames(peer), "prompt never entered server_requests.send_async"
    asked = []
    mod.register_gateway_notify(SESSION, lambda data: asked.append(data))
    try:
        _manual(monkeypatch, 0.05)
        again = mod.check_all_command_guards("rm -rf /var/cache/probe", "local")
    finally:
        mod.unregister_gateway_notify(SESSION)
    assert asked, "once persisted and skipped the next prompt"
    assert again.get("outcome") == "timeout"


def test_explicit_denial_arrives_through_approval_respond(approval_env, monkeypatch):
    mod = approval_env
    _manual(monkeypatch, 5)
    peer = _Peer()
    server = _bind_desktop_session(peer)
    mod.register_gateway_notify(SESSION, lambda data: server._emit_approval_request(UI, data))
    responder = _respond_with_rpc(server, peer, "deny")
    try:
        result = _wait_on_loop(mod, "rm -rf /var/cache/probe")
    finally:
        responder.join(timeout=5)
        mod.unregister_gateway_notify(SESSION)
        _unbind(server)
    assert result["approved"] is False
    assert result.get("outcome") == "denied"
    assert result.get("user_consent") is False


def test_silence_is_timeout_not_consent(approval_env, monkeypatch):
    mod = approval_env
    _manual(monkeypatch, 0.05)
    peer = _Peer()
    server = _bind_desktop_session(peer)
    mod.register_gateway_notify(SESSION, lambda data: server._emit_approval_request(UI, data))
    try:
        result = _wait_on_loop(mod, "rm -rf /var/cache/probe")
    finally:
        mod.unregister_gateway_notify(SESSION)
        _unbind(server)
    assert result["approved"] is False
    assert result.get("outcome") == "timeout"
    assert "Silence is not consent" in result["message"]
    assert _approval_frames(peer)


def test_stop_on_the_waiter_withdraws_without_user_deny(approval_env, monkeypatch):
    mod = approval_env
    _manual(monkeypatch, 5)
    peer = _Peer()
    server = _bind_desktop_session(peer)
    mod.register_gateway_notify(SESSION, lambda data: server._emit_approval_request(UI, data))

    async def on_loop():
        set_interrupt(True, reason="turn interrupted")
        try:
            return mod.check_all_command_guards("rm -rf /var/cache/probe", "local")
        finally:
            set_interrupt(False)

    try:
        result = asyncio.run(on_loop())
    finally:
        mod.unregister_gateway_notify(SESSION)
        _unbind(server)
    assert result["approved"] is False
    assert result.get("outcome") == "cancelled"
    assert result.get("user_consent") is False
    assert any(
        frame.get("method") == "event" and (frame.get("params") or {}).get("type") == "request.cancel"
        for frame in peer.frames
    )


def test_wrong_session_and_late_frame_do_not_approve(approval_env, monkeypatch):
    mod = approval_env
    _manual(monkeypatch, 0.05)
    peer = _Peer()
    server = _bind_desktop_session(peer)
    server._sessions["other-ui"] = {
        "session_key": "other-session",
        "transport": peer,
        "history": [],
        "history_lock": threading.Lock(),
    }
    mod.register_gateway_notify(SESSION, lambda data: server._emit_approval_request(UI, data))
    try:
        result = _wait_on_loop(mod, "rm -rf /var/cache/probe")
        frame = _approval_frames(peer)[-1]
        wrong = server.handle_request({
            "id": "wrong-session",
            "method": "approval.respond",
            "params": {
                "session_id": "other-ui",
                "request_id": frame["params"]["request_id"],
                "choice": "once",
            },
        })
        from tui_gateway import server_requests

        late = server_requests.resolve_response({"id": frame["id"], "result": {"choice": "once"}})
    finally:
        mod.unregister_gateway_notify(SESSION)
        _unbind(server)
    assert result.get("outcome") == "timeout"
    assert wrong["result"]["resolved"] == 0
    assert late is False
    assert not mod._session_approved.get(SESSION)


def test_closed_transport_withdraws_instead_of_waiting_out_silence(approval_env, monkeypatch):
    """A write that returns false is a delivery failure, not user silence."""
    mod = approval_env
    _manual(monkeypatch, 5)
    peer = _Peer(deliver=False)
    server = _bind_desktop_session(peer)
    mod.register_gateway_notify(SESSION, lambda data: server._emit_approval_request(UI, data))
    started = time.monotonic()
    try:
        result = mod.check_all_command_guards("rm -rf /var/cache/probe", "local")
    finally:
        mod.unregister_gateway_notify(SESSION)
        _unbind(server)
    assert time.monotonic() - started < 1
    assert result["approved"] is False
    assert result.get("user_consent") is False
    assert result.get("outcome") != "denied"


def test_pinned_transport_false_write_returns_without_waiting(monkeypatch):
    """The selected writer is transport.write when a socket is pinned. Settle on its False, not a second frame()."""
    from tui_gateway import server_requests

    frames: list[dict] = []

    class _Dead:
        def write(self, frame: dict) -> bool:
            frames.append(frame)
            return False

    monkeypatch.setattr(server_requests, "_answerable", lambda sid: True)
    started = time.monotonic()
    result = server_requests.send(
        "preview.act",
        "sid-dead",
        {"action": "vault", "vault": {"operation": "evaluate", "expression": "secret-script"}},
        timeout=2,
        transport=_Dead(),
    )
    elapsed = time.monotonic() - started
    assert result is None
    assert elapsed < 0.5, "false write still blocked the caller for the deadline"
    assert frames[0]["params"]["vault"]["expression"] == "secret-script"
    assert all(req.sid != "sid-dead" for req in server_requests._open.values())


def test_bound_sink_false_write_settles_the_async_request(monkeypatch):
    from tui_gateway import server_requests

    seen = []
    monkeypatch.setattr(server_requests, "_write", lambda frame: False)
    monkeypatch.setattr(server_requests, "_answerable", lambda sid: True)
    server_requests.send_async(
        "approval", "sid-1", {"request_id": "req-1", "command": "echo hi"}, seen.append,
    )
    assert seen == [None]
    assert all(req.sid != "sid-1" for req in server_requests._open.values())
