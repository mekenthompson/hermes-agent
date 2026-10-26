"""Short-lived native clients preserve a shared browser and a human takeover.

Run real launch records, holder leases, process exit and orphan reaping against
the test Chromium process. No production browser or profile is accessed.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from tests.tools.browser_launcher_fakes import kill_fake, write_fake_chromium
from tools import browser_launcher as launcher
from tools.bot_desktop import lease

pytestmark = [pytest.mark.platforms("linux"), pytest.mark.live_system_guard_bypass]
REPO = Path(__file__).resolve().parents[2]

CLIENT = textwrap.dedent("""\
    import os, sys
    from pathlib import Path
    from tools import browser_launcher as launcher, browser_session as session
    from tools.bot_desktop import lease
    from hermes_constants import hermes_home_key
    from tests.tools.browser_launcher_fakes import install_fake_chromium_identity
    binary, profile, mode = sys.argv[1:]
    install_fake_chromium_identity(Path(binary))
    browser, error = launcher.launch_chromium('client', executable=binary, user_data_dir=profile,
        ephemeral=False, headed=False, env=dict(os.environ), timeout=20)
    if error:
        raise RuntimeError(error)
    session._publish('client', {'launcher_key': 'client', 'features': {'local': True},
                               'cdp_url': browser.cdp_url}, hermes_home_key())
    Path(profile, 'unrelated-tab').write_text('human work')
    if mode in ('human', 'detach-human'):
        lease.acquire('test-viewer')
        refused = session.run_fenced({'features': {'local': True}}, lambda: {'unexpected': True})
        assert refused.get('code') == 'human_has_control'
    if mode in ('detach', 'detach-human'):
        assert session.detach_shared_browsers() == 1
    print(browser.pid, flush=True)
""")


@pytest.mark.parametrize("mode", ["detach", "human", "detach-human"])
def test_only_client_exit_and_reaper_preserve_browser(tmp_path, mode):
    binary = write_fake_chromium(tmp_path / "chromium")
    home, profile = tmp_path / "home", tmp_path / "profile"
    result = subprocess.run([sys.executable, "-c", CLIENT, str(binary), str(profile), mode],
                            cwd=REPO, env={**os.environ, "HERMES_HOME": str(home)},
                            capture_output=True, text=True, timeout=45, check=False)
    assert result.returncode == 0, result.stderr
    pid = int(result.stdout.strip())
    try:
        assert launcher._pid_alive(pid), "client exit terminated a shared browser"
        launcher.reap_orphaned_browsers(homes=[str(home)])
        assert launcher._pid_alive(pid), "orphan cleanup ignored retained/human ownership"
        assert (profile / "unrelated-tab").read_text() == "human work"
        if mode == "human":
            lease.release("test-viewer", profile_key=str(home))
            launcher.reap_orphaned_browsers(homes=[str(home)])
            assert not launcher._pid_alive(pid), "deferred cleanup did not resume after handback"
    finally:
        kill_fake(pid, binary)


def test_cleanup_uses_browser_home_instead_of_launch_profile(tmp_path, monkeypatch):
    held_home, other_home = tmp_path / "held", tmp_path / "other"
    lease.acquire("test-viewer", profile_key=str(held_home))
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    browser = launcher.LaunchedBrowser("client", 123, 456, "ws://unused", "http://unused",
                                       str(tmp_path / "profile"), True, True, str(held_home))
    monkeypatch.setitem(launcher._browsers, "client", browser)
    stopped = []
    monkeypatch.setattr(launcher, "_terminate", lambda *args: stopped.append(args))
    launcher.stop_browser("client")
    assert stopped == [], "cleanup used the launch profile's lease"


def test_shared_holder_handover_keeps_launch_home_human_fence(tmp_path, monkeypatch):
    from dataclasses import replace

    held_home, other_home = tmp_path / "held", tmp_path / "other"
    lease.acquire("test-viewer", profile_key=str(held_home))
    browser = launcher.LaunchedBrowser("client", 123, 456, "ws://unused", "http://unused",
                                       str(tmp_path / "profile"), True, False, str(held_home))
    launcher._write_record(browser)
    attacher = replace(browser, owned=False, home_key=str(other_home))
    monkeypatch.setitem(launcher._browsers, "client", attacher)
    stopped = []
    monkeypatch.setattr(launcher, "_terminate", lambda *args: stopped.append(args))
    launcher.stop_browser("client")
    assert stopped == [], "holder handover changed the browser display's owning profile"
