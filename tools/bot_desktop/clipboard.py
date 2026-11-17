"""Copy-out of the Bot Screen: the text clipboard of a profile's display, for the lease holder only.

The screen's Xvnc never sends cut text (``-SendCutText=0``: a watcher must not receive the holder's
clipboard), so there is no RFB path out. The holder pulls instead: this reads the X CLIPBOARD selection of
the profile's published display with :mod:`tools.bot_desktop.x11_selection`. WHO may pull is decided by the
caller (``display.clipboard.read`` checks the lease); this module only knows WHERE the display is: on the
gateway host, or inside the terminal-backend sandbox that hosts it.
"""

from __future__ import annotations

from tools.bot_desktop import runtime
from tools.bot_desktop.x11_selection import ClipboardError, read_clipboard_text

__all__ = ["ClipboardError", "read_text"]


def read_text() -> str:
    """The text in the screen's CLIPBOARD selection ("" when nothing is selected). Raises
    :class:`ClipboardError` (``not_running``, ``unavailable``, ``timeout``, ``too_large``)."""
    published = runtime.published_env()
    display, xauthority = published.get("DISPLAY"), published.get("XAUTHORITY")
    if not display or not xauthority:
        raise ClipboardError("not_running", "this profile's Bot Desktop is not running")
    if runtime.sandbox_screen_running():
        from tools.bot_desktop import sandbox_host
        sandbox = runtime._owned_sandbox_env()
        if sandbox is None:
            raise ClipboardError("not_running", "the sandbox hosting this screen is not running")
        return sandbox_host.read_clipboard(sandbox, display, xauthority)
    return read_clipboard_text(display, xauthority)
