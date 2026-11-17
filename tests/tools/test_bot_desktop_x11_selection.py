"""``x11_selection.read_clipboard_text`` against a real Xvnc: the text a program on the Bot Screen copied is
what the lease holder's "Copy from screen" gets, and every way that can go wrong ends in a typed refusal."""

from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path

import pytest

from tests.tools.x11_selection_fixture import XVNC, Owner, Screen
from tools.bot_desktop import x11_selection as x11

pytestmark = pytest.mark.skipif(XVNC is None, reason="Xvnc is not installed")


@pytest.fixture
def screen(tmp_path):
    for _ in range(20):  # a display number another X server may already hold
        try:
            s = Screen(tmp_path, random.randint(300, 900))
            break
        except RuntimeError:
            continue
    else:
        pytest.skip("no free X display number")
    yield s
    s.close()


@pytest.fixture
def owner(screen):
    owners = []

    def make(*args, **kwargs):
        owners.append(Owner(screen, *args, **kwargs))
        return owners[-1]

    yield make
    for o in owners:
        o.close()


def test_returns_the_text_the_screen_copied(screen, owner):
    text = "héllo wörld ✓\nsecond line"
    owner(text)
    assert x11.read_clipboard_text(screen.display, str(screen.xauthority)) == text


def test_nothing_selected_is_empty_text(screen):
    assert x11.read_clipboard_text(screen.display, str(screen.xauthority)) == ""


def test_falls_back_to_legacy_string_when_the_owner_has_no_utf8(screen, owner):
    owner(None, legacy="café".encode("latin-1"))
    assert x11.read_clipboard_text(screen.display, str(screen.xauthority)) == "café"


def test_incremental_transfer_is_refused_as_too_large(screen, owner):
    # A selection bigger than one X request can carry is sent in INCR chunks: refused, never truncated.
    owner("x" * 10, incr=True)
    with pytest.raises(x11.ClipboardError) as exc:
        x11.read_clipboard_text(screen.display, str(screen.xauthority))
    assert exc.value.code == "too_large"


def test_wrong_cookie_is_unavailable(screen, tmp_path):
    from tests.tools.x11_selection_fixture import write_xauthority
    bad = tmp_path / "bad-auth"
    write_xauthority(bad, screen.number, b"\0" * 16)
    with pytest.raises(x11.ClipboardError) as exc:
        x11.read_clipboard_text(screen.display, str(bad))
    assert exc.value.code == "unavailable"


def test_missing_display_or_cookie_is_unavailable(tmp_path):
    with pytest.raises(x11.ClipboardError) as exc:
        x11.read_clipboard_text(":999", str(tmp_path / "nope"))
    assert exc.value.code == "unavailable"
    with pytest.raises(x11.ClipboardError) as exc:
        x11.read_clipboard_text("example.com:0", str(tmp_path / "nope"))
    assert exc.value.code == "unavailable"


def test_script_form_used_inside_a_sandbox_prints_one_json_object(screen, owner):
    """A sandbox runs the module source as ``python3 -c <source> DISPLAY XAUTHORITY``."""
    owner("from the sandbox")
    source = Path(x11.__file__).read_text(encoding="utf-8")
    out = subprocess.run([sys.executable, "-c", source, screen.display, str(screen.xauthority)],
                         capture_output=True, text=True, timeout=30, check=True).stdout
    assert out == '{"text": "from the sandbox"}'
