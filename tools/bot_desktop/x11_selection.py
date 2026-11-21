"""Read the text CLIPBOARD selection of an X display: a minimal, dependency-free X11 client.

Why this exists: the Bot Screen's Xvnc runs ``-SendCutText=0`` (a watcher must never be sent the holder's
clipboard), so nothing copied on the screen ever reaches the viewer over RFB. The holder's "Copy from
screen" is therefore a pull on the X selection itself. ``xclip``/``xsel`` and ``python-xlib`` are not part
of the Bot Screen package list, and Pillow's XCB binding only grabs pixels, so this speaks the four requests
it needs (InternAtom, CreateWindow, ConvertSelection, GetProperty) over the display's Unix socket.

Self-contained on purpose (stdlib only, Python 3.6+ syntax, no Hermes imports): a screen hosted inside a
terminal-backend sandbox runs this file's source with that sandbox's ``python3`` (``python3 -c <source>
DISPLAY XAUTHORITY``, the same shape as the thumbnail grab) because its X socket and cookie live there.
"""

import json
import os
import socket
import struct
import sys
import time

MAX_TEXT_BYTES = 262144  # the same bound as a client cut-text on the RFB bridge (rfb_filter._MAX_CUT_TEXT)
DEFAULT_TIMEOUT_S = 3.0
_PROPERTY_NAME = "HERMES_CLIPBOARD"
_X_DIR = "/tmp/.X11-unix"  # no-tmp: ok — the X11 protocol fixes where a display's socket lives
_SELECTION_NOTIFY = 31
_ATOM_STRING = 31  # predefined by the protocol


class ClipboardError(Exception):
    """``code`` is one of ``not_running`` (no screen to read), ``unavailable`` (cannot reach or authenticate
    to the display), ``timeout`` (the selection owner never answered) or ``too_large`` (more than
    MAX_TEXT_BYTES, or an INCR transfer)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _pad4(n):
    return (n + 3) & ~3


def _display_number(display):
    host, sep, rest = display.rpartition(":")
    number = rest.split(".")[0]
    if not sep or host not in ("", "unix") or not number.isdigit():
        raise ClipboardError("unavailable", f"only a local X display is supported: {display!r}")
    return int(number)


def _read_cookie(xauthority, number):
    """The MIT-MAGIC-COOKIE-1 for display ``number`` out of an Xauthority file (the launcher writes one
    cookie per profile, so the host-name column is not needed to pick it)."""
    try:
        with open(xauthority, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise ClipboardError("unavailable", f"cannot read XAUTHORITY: {exc}")
    pos = 0
    try:
        while pos < len(data):
            pos += 2  # family
            fields = []
            for _ in range(4):  # address, number, name, data
                (size,) = struct.unpack_from(">H", data, pos)
                fields.append(data[pos + 2:pos + 2 + size])
                pos += 2 + size
            if fields[1] == str(number).encode() and fields[2] == b"MIT-MAGIC-COOKIE-1":
                return fields[3]
    except struct.error:
        pass
    raise ClipboardError("unavailable", f"no cookie for display :{number} in XAUTHORITY")


def _connect(number, deadline):
    last = None
    for address in (f"{_X_DIR}/X{number}", f"\0{_X_DIR}/X{number}"):  # path, then Linux abstract
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(max(deadline - time.monotonic(), 0.1))
        try:
            sock.connect(address)
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    raise ClipboardError("unavailable", f"cannot connect to display :{number}: {last}")


class _Wire:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline

    def recv(self, n):
        buf = b""
        while len(buf) < n:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ClipboardError("timeout", "the clipboard owner did not answer in time")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(n - len(buf))
            except socket.timeout:  # noqa: UP041 -- an alias of TimeoutError only from Python 3.10; sandboxes may be older
                raise ClipboardError("timeout", "the clipboard owner did not answer in time")
            if not chunk:
                raise ClipboardError("unavailable", "the X server closed the connection")
            buf += chunk
        return buf

    def send(self, data):
        self.sock.settimeout(max(self.deadline - time.monotonic(), 0.1))
        self.sock.sendall(data)

    def message(self):
        """The next 32-byte-aligned server message: ``(kind, bytes)`` with kind reply/event; errors raise."""
        head = self.recv(32)
        kind = head[0]
        if kind == 0:
            raise ClipboardError("unavailable", f"X protocol error {head[1]}")
        if kind == 1:
            (extra,) = struct.unpack_from("<I", head, 4)
            return "reply", head + self.recv(extra * 4)
        return "event", head

    def reply(self):
        while True:
            kind, data = self.message()  # events may interleave with a reply; only the reply is wanted here
            if kind == "reply":
                return data


def _handshake(wire, cookie):
    name = b"MIT-MAGIC-COOKIE-1"
    wire.send(struct.pack("<BxHHHHxx", ord("l"), 11, 0, len(name), len(cookie))
              + name + b"\0" * (_pad4(len(name)) - len(name)) + cookie + b"\0" * (_pad4(len(cookie)) - len(cookie)))
    head = wire.recv(8)
    body = wire.recv(struct.unpack_from("<H", head, 6)[0] * 4)
    if head[0] != 1:
        raise ClipboardError("unavailable", "the X server refused the connection")
    resource_base = struct.unpack_from("<I", body, 4)[0]
    vendor_len, = struct.unpack_from("<H", body, 16)
    formats = body[21]
    (root,) = struct.unpack_from("<I", body, 32 + _pad4(vendor_len) + 8 * formats)
    return resource_base, root


def _intern(wire, name):
    raw = name.encode()
    wire.send(struct.pack("<BBHHxx", 16, 0, 2 + _pad4(len(raw)) // 4, len(raw)) + raw + b"\0" * (_pad4(len(raw)) - len(raw)))
    return struct.unpack_from("<I", wire.reply(), 8)[0]


def _await_selection_notify(wire):
    """The property the owner wrote its answer to (0 when it refused the target)."""
    while True:
        kind, data = wire.message()
        if kind == "event" and data[0] & 0x7F == _SELECTION_NOTIFY:
            return struct.unpack_from("<I", data, 20)[0]


def _fetch(wire, window, clipboard, target, prop, incr):
    wire.send(struct.pack("<BxHIIIII", 24, 6, window, clipboard, target, prop, 0))  # CurrentTime
    if _await_selection_notify(wire) == 0:
        return None
    # delete=1: the owner's transfer is consumed; ask for one word past the cap to detect an oversize value.
    wire.send(struct.pack("<BBHIIIII", 20, 1, 6, window, prop, 0, 0, MAX_TEXT_BYTES // 4 + 1))
    data = wire.reply()
    fmt, ptype, after, items = data[1], *struct.unpack_from("<III", data, 8)
    if ptype == incr or after:
        raise ClipboardError("too_large", f"the clipboard holds more than {MAX_TEXT_BYTES} bytes")
    value = data[32:32 + items * (fmt // 8)]
    if len(value) > MAX_TEXT_BYTES:
        raise ClipboardError("too_large", f"the clipboard holds more than {MAX_TEXT_BYTES} bytes")
    return value


def read_clipboard_text(display, xauthority, timeout=DEFAULT_TIMEOUT_S):
    """The text in the CLIPBOARD selection of ``display`` ("" when nothing is selected). Raises
    :class:`ClipboardError`. Asks for UTF8_STRING first, then the legacy STRING (Latin-1)."""
    deadline = time.monotonic() + timeout
    number = _display_number(display)
    cookie = _read_cookie(xauthority, number)
    sock = _connect(number, deadline)
    try:
        wire = _Wire(sock, deadline)
        base, root = _handshake(wire, cookie)
        clipboard, utf8, incr, prop = (_intern(wire, n) for n in ("CLIPBOARD", "UTF8_STRING", "INCR", _PROPERTY_NAME))
        window = base | 1
        # InputOnly 1x1 window: only a SelectionNotify destination, never mapped, never visible.
        wire.send(struct.pack("<BBHIIhhHHHHII", 1, 0, 8, window, root, 0, 0, 1, 1, 0, 2, 0, 0))
        value = _fetch(wire, window, clipboard, utf8, prop, incr)
        if value is not None:
            return value.decode("utf-8", "replace")
        value = _fetch(wire, window, clipboard, _ATOM_STRING, prop, incr)
        return "" if value is None else value.decode("latin-1")
    finally:
        sock.close()


def main(argv):
    """``python3 -c <this file> DISPLAY XAUTHORITY``: one JSON object on stdout, exit 0 either way."""
    try:
        out = {"text": read_clipboard_text(argv[1], argv[2])}
    except ClipboardError as exc:
        out = {"error": exc.code, "message": str(exc)}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv)
