"""Real-network regression coverage for stock Desktop's reusable ``?token=`` WS URL."""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time

import pytest
from websockets.sync.client import connect
from websockets.typing import Origin

from hermes_cli import web_server


def _wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError("uvicorn did not start")


def _ping(ws, request_id: str) -> None:
    ws.send(json.dumps({"jsonrpc": "2.0", "method": "ping", "params": {}, "id": request_id}))
    while True:
        frame = json.loads(ws.recv(timeout=10))
        if frame.get("id") == request_id:
            assert frame == {"jsonrpc": "2.0", "result": {"pong": True}, "id": request_id}
            return


@pytest.mark.timeout(30)
@pytest.mark.parametrize("log_level", [logging.INFO, logging.DEBUG])
def test_explicit_token_standard_url_connects_reconnects_and_redacts_protocol_logs(
    monkeypatch, tmp_path, caplog, capfd, log_level
):
    """Exercise actual Uvicorn + /api/ws JSON-RPC rather than TestClient."""
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", token)
    monkeypatch.setattr(web_server, "_SESSION_TOKEN_IS_EXPLICIT", True)
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(web_server.app.state, "trusted_public_hosts", frozenset(), raising=False)

    config, server = web_server._build_uvicorn_server("127.0.0.1", 0)
    config.lifespan = "off"
    assert config.access_log is False
    caplog.set_level(log_level, logger="uvicorn.error")
    thread = threading.Thread(target=server.run, name="explicit-token-uvicorn-test", daemon=True)
    thread.start()
    try:
        _wait_until(lambda: bool(server.started and server.servers))
        port = server.servers[0].sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/api/ws?token={token}"
        origin = Origin(f"http://127.0.0.1:{port}")
        # Match stock Desktop's reusable URL transport for first connection and reconnect.
        with connect(url, origin=origin, open_timeout=10) as initial:
            _ping(initial, "initial")
        with connect(url, origin=origin, open_timeout=10) as reconnect:
            _ping(reconnect, "reconnect")
        for rejected_url, rejected_origin in (
            (f"ws://127.0.0.1:{port}/api/ws", origin),
            (f"ws://127.0.0.1:{port}/api/ws?token=other-gateway-test-only", origin),
            (f"{url}&token={token}", origin),
            (f"{url}&ticket=", origin),
            (url, "https://untrusted-origin.invalid"),
        ):
            from websockets.exceptions import InvalidStatus
            with pytest.raises(InvalidStatus) as rejected:
                with connect(rejected_url, origin=Origin(rejected_origin), open_timeout=10):
                    pytest.fail("invalid credential/origin was accepted")
            assert rejected.value.response.status_code == 403
        monkeypatch.setattr(web_server, "_SESSION_TOKEN_IS_EXPLICIT", False)
        with pytest.raises(InvalidStatus) as generated:
            with connect(url, origin=origin, open_timeout=10):
                pytest.fail("generated process token authenticated gated gateway")
        assert generated.value.response.status_code == 403
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    assert not thread.is_alive()
    captured = capfd.readouterr()
    rendered = caplog.text + captured.out + captured.err
    assert "[accepted]" in rendered
    assert token not in rendered
