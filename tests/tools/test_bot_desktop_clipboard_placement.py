"""``clipboard.read_text`` finds the screen where the profile placed it: on the gateway host, or inside the
terminal-backend sandbox. Both run against a real Xvnc."""

from __future__ import annotations

import random
import subprocess
import sys

import pytest

from tests.tools.x11_selection_fixture import XVNC, Owner, Screen
from tools.bot_desktop import clipboard, runtime, sandbox_host

needs_xvnc = pytest.mark.skipif(XVNC is None, reason="Xvnc is not installed")


@pytest.fixture
def screen(tmp_path):
    for _ in range(20):
        try:
            s = Screen(tmp_path, random.randint(300, 900))
            break
        except RuntimeError:
            continue
    else:
        pytest.skip("no free X display number")
    owner = Owner(s, "from the screen")
    yield s
    owner.close()
    s.close()


def _publish(monkeypatch, screen):
    monkeypatch.setattr(runtime, "published_env",
                        lambda: {"DISPLAY": screen.display, "XAUTHORITY": str(screen.xauthority)})


def test_a_stopped_screen_is_refused(monkeypatch):
    monkeypatch.setattr(runtime, "published_env", dict)
    with pytest.raises(clipboard.ClipboardError) as exc:
        clipboard.read_text()
    assert exc.value.code == "not_running"


@needs_xvnc
def test_gateway_hosted_screen(monkeypatch, screen):
    _publish(monkeypatch, screen)
    monkeypatch.setattr(runtime, "sandbox_screen_running", lambda: False)
    assert clipboard.read_text() == "from the screen"


@needs_xvnc
def test_sandbox_hosted_screen_is_read_with_the_sandboxs_python(monkeypatch, screen):
    _publish(monkeypatch, screen)
    monkeypatch.setattr(runtime, "sandbox_screen_running", lambda: True)
    monkeypatch.setattr(runtime, "_owned_sandbox_env", lambda: object())
    monkeypatch.setattr(sandbox_host, "_user_for", lambda env: None)
    seen = []

    def run_in(env, argv, **kwargs):
        seen.append(argv[0])
        return subprocess.run([sys.executable, *argv[1:]], capture_output=True, timeout=30, check=False)

    monkeypatch.setattr(sandbox_host.streams, "run_in", run_in)
    assert clipboard.read_text() == "from the screen"
    assert seen == ["python3"]


def test_a_sandbox_that_cannot_run_python_is_unavailable(monkeypatch):
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":20", "XAUTHORITY": "/x"})
    monkeypatch.setattr(runtime, "sandbox_screen_running", lambda: True)
    monkeypatch.setattr(runtime, "_owned_sandbox_env", lambda: object())
    monkeypatch.setattr(sandbox_host, "_user_for", lambda env: None)
    monkeypatch.setattr(sandbox_host.streams, "run_in",
                        lambda *a, **k: subprocess.CompletedProcess(a, 127, b"", b"python3: not found"))
    with pytest.raises(clipboard.ClipboardError) as exc:
        clipboard.read_text()
    assert exc.value.code == "unavailable"
