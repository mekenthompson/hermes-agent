"""display.clipboard.read: the screen's clipboard goes to the viewer holding the lease and to no one else.

Xvnc runs -SendCutText=0 so a watcher never receives the holder's clipboard; the holder pulls it here.
"""

from __future__ import annotations

import pytest

from tests.bot_desktop_lease import reset_lease
from tools.bot_desktop import clipboard as bd_clipboard


@pytest.fixture
def lease_mod():
    from tools.bot_desktop import lease
    reset_lease()
    yield lease
    reset_lease()


@pytest.fixture
def reads(monkeypatch):
    """Records every read of the screen's clipboard; answers with text no refused caller may ever see."""
    calls = []
    monkeypatch.setattr(bd_clipboard, "read_text", lambda: calls.append(1) or "copied-on-the-screen")
    return calls


def _call(method, params):
    from tui_gateway import server
    return server.handle_request({"jsonrpc": "2.0", "id": 3, "method": method, "params": params})


@pytest.fixture
def viewer(monkeypatch, tmp_path):
    """A viewer id minted for this connection by display.observe."""
    from tools.bot_desktop import runtime
    monkeypatch.setattr(runtime, "rfb_socket_path", lambda: tmp_path / "rfb.sock")
    return _call("display.observe", {})["result"]["viewer_id"]


def test_the_holder_gets_the_screens_text(lease_mod, reads, viewer):
    lease_mod.acquire(viewer)
    assert _call("display.clipboard.read", {"viewer_id": viewer})["result"] == {"text": "copied-on-the-screen"}


@pytest.mark.parametrize("setup", ["agent_holds", "another_viewer_holds"])
def test_a_viewer_who_is_not_in_control_is_refused_and_nothing_is_read(lease_mod, reads, viewer, setup):
    if setup == "another_viewer_holds":
        lease_mod.acquire("someone-elses-viewer-id")
    resp = _call("display.clipboard.read", {"viewer_id": viewer})
    assert resp["error"]["data"]["code"] == "not_in_control"
    assert "copied-on-the-screen" not in str(resp)
    assert reads == []


def test_a_viewer_id_this_connection_never_minted_is_refused_even_when_it_holds(lease_mod, reads, viewer):
    lease_mod.acquire("invented-id")
    resp = _call("display.clipboard.read", {"viewer_id": "invented-id"})
    assert resp["error"]["data"]["code"] == "viewer_mismatch"
    assert reads == []


def test_a_takeover_during_the_read_drops_the_text(lease_mod, monkeypatch, viewer):
    lease_mod.acquire(viewer)

    def read_while_someone_takes_over():
        lease_mod.acquire("the-next-holder")
        return "text that may now be the next holder's"

    monkeypatch.setattr(bd_clipboard, "read_text", read_while_someone_takes_over)
    resp = _call("display.clipboard.read", {"viewer_id": viewer})
    assert resp["error"]["data"]["code"] == "not_in_control"
    assert "next holder" not in str(resp)


@pytest.mark.parametrize("code", ["not_running", "unavailable", "timeout", "too_large"])
def test_read_failures_reach_the_pane_as_typed_codes(lease_mod, monkeypatch, viewer, code):
    lease_mod.acquire(viewer)

    def fail():
        raise bd_clipboard.ClipboardError(code, "nope")

    monkeypatch.setattr(bd_clipboard, "read_text", fail)
    assert _call("display.clipboard.read", {"viewer_id": viewer})["error"]["data"]["code"] == code
