"""A real Xvnc plus a raw-protocol selection owner, for tests of ``tools.bot_desktop.x11_selection``.

Nothing here mocks the X wire: the server is the installed ``Xvnc`` and the owner speaks the protocol
through the module's own handshake. Skipped by callers when ``Xvnc`` is not installed.
"""

from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

from tools.bot_desktop import x11_selection as x11

XVNC = shutil.which("Xvnc")
_SELECTION_REQUEST = 30


def write_xauthority(path: Path, number: int, cookie: bytes) -> None:
    def field(b: bytes) -> bytes:
        return struct.pack(">H", len(b)) + b

    # A non-matching record first: the reader must pick the entry by display number.
    other = struct.pack(">H", 256) + field(b"h") + field(b"7") + field(b"MIT-MAGIC-COOKIE-1") + field(b"x" * 16)
    mine = struct.pack(">H", 256) + field(b"h") + field(str(number).encode()) + field(b"MIT-MAGIC-COOKIE-1") + field(cookie)
    path.write_bytes(other + mine)
    path.chmod(0o600)


class Screen:
    """Xvnc on a private display number, cookie-protected."""

    def __init__(self, tmp: Path, number: int):
        self.number = number
        self.display = f":{number}"
        self.xauthority = tmp / "Xauthority"
        write_xauthority(self.xauthority, number, os.urandom(16))
        self.proc = subprocess.Popen(
            [str(XVNC), self.display, "-geometry", "320x200", "-SecurityTypes", "None", "-nolisten", "tcp",
             "-auth", str(self.xauthority), "-rfbunixpath", str(tmp / "rfb.sock")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.monotonic() + 10
        while not Path(f"{x11._X_DIR}/X{number}").exists():
            if time.monotonic() > end or self.proc.poll() is not None:
                raise RuntimeError("Xvnc did not come up")
            time.sleep(0.05)

    def close(self) -> None:
        self.proc.terminate()
        self.proc.wait(10)


class Owner:
    """Owns CLIPBOARD and serves ``text`` as UTF8_STRING (and ``legacy`` as STRING when given)."""

    def __init__(self, screen: Screen, text: str | None, legacy: bytes | None = None, incr: bool = False):
        self.text, self.legacy, self.incr = text, legacy, incr
        deadline = time.monotonic() + 10
        self.sock = x11._connect(screen.number, deadline)
        self.wire = x11._Wire(self.sock, deadline + 3600)
        base, root = x11._handshake(self.wire, x11._read_cookie(str(screen.xauthority), screen.number))
        self.window = base | 2
        self.wire.send(struct.pack("<BBHIIhhHHHHII", 1, 0, 8, self.window, root, 0, 0, 1, 1, 0, 2, 0, 0))
        self.clipboard = x11._intern(self.wire, "CLIPBOARD")
        self.utf8 = x11._intern(self.wire, "UTF8_STRING")
        self.incr_atom = x11._intern(self.wire, "INCR")
        self.wire.send(struct.pack("<BxHIII", 22, 4, self.window, self.clipboard, 0))  # SetSelectionOwner
        self.requests = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            while True:
                kind, ev = self.wire.message()
                if kind == "event" and ev[0] & 0x7F == _SELECTION_REQUEST:
                    self.requests += 1
                    requestor, selection, target, prop = struct.unpack_from("<IIII", ev, 12)
                    self._answer(requestor, selection, target, prop)
        except (x11.ClipboardError, OSError):
            return  # the socket was closed by close()

    def _answer(self, requestor: int, selection: int, target: int, prop: int) -> None:
        if self.text is not None and target == self.utf8:
            data, ptype = self.text.encode(), (self.incr_atom if self.incr else self.utf8)
        elif self.legacy is not None and target == x11._ATOM_STRING:
            data, ptype = self.legacy, x11._ATOM_STRING
        else:
            data, ptype, prop = b"", 0, 0  # refuse the target
        if prop:
            padded = data + b"\0" * (x11._pad4(len(data)) - len(data))
            self.wire.send(struct.pack("<BBHIIIB3xI", 18, 0, 6 + len(padded) // 4, requestor, prop, ptype, 8, len(data))
                           + padded)
        notify = struct.pack("<BxHIIIII", 31, 0, 0, requestor, selection, target, prop) + b"\0" * 8
        self.wire.send(struct.pack("<BBHII", 25, 0, 11, requestor, 0) + notify)

    def close(self) -> None:
        self.sock.close()
        self.thread.join(5)
