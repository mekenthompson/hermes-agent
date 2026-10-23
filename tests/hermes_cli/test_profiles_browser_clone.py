"""A cloned profile must not attach to the source profile's live browser."""

import http.server
import json
import os
import socket
import threading
from pathlib import Path

import psutil
import pytest

from hermes_cli.profiles import create_profile
from tools.bot_desktop.browser import running_instance_cdp_port


@pytest.mark.platforms("linux")
def test_clone_all_does_not_attach_to_the_source_profiles_live_browser(tmp_path, monkeypatch):
    """The source stays attachable while a clone cannot inherit its CDP endpoint."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    (default_home / "config.yaml").write_text("model: test", encoding="utf-8")
    browser_profile = default_home / "bot-desktop" / "browser-profile"
    browser_profile.mkdir(parents=True)

    class DevToolsHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/json/version":
                self.send_error(404)
                return
            body = json.dumps({"webSocketDebuggerUrl":
                               f"ws://127.0.0.1:{self.server.server_port}/devtools/browser/source"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    # The listener belongs to pytest; present its executable and argv as Chromium
    # while keeping the real UID and socket ownership checks in the probe.
    real_process = psutil.Process

    class ChromiumProcess:
        def __init__(self, pid):
            self._real = real_process(pid)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def exe(self):
            return "/usr/bin/chromium"

        def cmdline(self):
            return ["/usr/bin/chromium", f"--user-data-dir={browser_profile}", "--remote-debugging-port=0"]

    monkeypatch.setattr(psutil, "Process", lambda pid: ChromiumProcess(pid) if pid == os.getpid() else real_process(pid))
    with http.server.HTTPServer(("127.0.0.1", 0), DevToolsHandler) as listener:
        worker = threading.Thread(target=listener.serve_forever)
        worker.start()
        try:
            port = listener.server_port
            (browser_profile / "DevToolsActivePort").write_text(
                f"{port}\n/devtools/browser/source\n", encoding="utf-8")
            (browser_profile / "SingletonLock").symlink_to(f"{socket.gethostname()}-{os.getpid()}")
            assert running_instance_cdp_port(str(browser_profile)) == port

            profile_dir = create_profile("coder", clone_all=True, no_alias=True)
            cloned_browser = profile_dir / "bot-desktop" / "browser-profile"

            assert running_instance_cdp_port(str(cloned_browser)) is None
            assert running_instance_cdp_port(str(browser_profile)) == port
        finally:
            listener.shutdown()
            worker.join()
