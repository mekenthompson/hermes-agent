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
    if mode == 'explicit-stop':
        launcher.stop_browser('client', force=True)
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
        if mode == "detach":
            later = subprocess.run([sys.executable, "-c", CLIENT, str(binary), str(profile), 'ordinary'],
                                   cwd=REPO, env={**os.environ, 'HERMES_HOME': str(home)},
                                   capture_output=True, text=True, timeout=45, check=True)
            assert int(later.stdout.strip()) == pid
            assert launcher._pid_alive(pid), 'a later ordinary client discarded durable retention'
            stopped = subprocess.run([sys.executable, "-c", CLIENT, str(binary), str(profile), 'explicit-stop'],
                                     cwd=REPO, env={**os.environ, 'HERMES_HOME': str(home)},
                                     capture_output=True, text=True, timeout=45, check=True)
            assert int(stopped.stdout.strip()) == pid
            assert not launcher._pid_alive(pid), 'explicit stop did not override retention'
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


def test_cleanup_guard_serializes_a_real_cross_process_takeover(tmp_path):
    home = tmp_path / 'home'
    code = textwrap.dedent("""\
        import fcntl, os, sys
        from tools.bot_desktop import lease
        path = lease._path(sys.argv[1]).with_suffix('.lock')
        with path.open('r+') as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('cleanup-admitted-first', flush=True)
            else:
                raise AssertionError('cleanup guard did not hold the takeover lock')
        lease.acquire('viewer', profile_key=sys.argv[1])
        print('takeover-completed', flush=True)
    """)
    process = None
    try:
        with lease.cleanup_guard(str(home)) as admitted:
            assert admitted
            process = subprocess.Popen([sys.executable, '-c', code, str(home)], cwd=REPO,
                                       env=dict(os.environ), stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            assert process.stdout.readline().strip() == 'cleanup-admitted-first'
            assert lease.get(str(home)).holder == lease.AGENT
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        assert stdout.strip() == 'takeover-completed'
        assert lease.get(str(home)).holder == lease.HUMAN
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_detaching_one_home_preserves_another_homes_live_shared_handle(tmp_path, monkeypatch):
    from tools import browser_session as session
    from tools.browser_session_lifecycle import cleanup_browser

    binary = write_fake_chromium(tmp_path / 'chromium')
    home_a, home_b = tmp_path / 'a', tmp_path / 'b'
    monkeypatch.setenv('HERMES_HOME', str(home_a))
    browser, error = launcher.launch_chromium('shared-homes', executable=str(binary),
                                             user_data_dir=str(tmp_path / 'profile'), ephemeral=False,
                                             headed=False, env=dict(os.environ), timeout=20)
    assert browser is not None, error
    info = {'launcher_key': 'shared-homes', 'cdp_url': browser.cdp_url, 'features': {'local': True}}
    session._publish('home-a-session', info, str(home_a))
    session._publish('home-b-session', info, str(home_b))
    try:
        assert session.detach_shared_browsers(str(home_a)) == 1
        assert 'home-b-session' in session._active_sessions
        assert launcher.get_browser('shared-homes') is browser
        cleanup_browser('home-b-session')
        assert launcher._pid_alive(browser.pid), 'later ordinary cleanup discarded requested retention'
        launcher.reap_orphaned_browsers(homes=[str(home_a), str(home_b)])
        assert launcher._pid_alive(browser.pid)
    finally:
        session._active_sessions.pop('home-a-session', None)
        session._active_sessions.pop('home-b-session', None)
        kill_fake(browser.pid, binary)


@pytest.mark.parametrize('failure', ['readiness-error', 'interrupted'])
def test_failed_launch_keeps_human_browser_until_handback(tmp_path, monkeypatch, failure):
    binary = write_fake_chromium(tmp_path / 'chromium')
    home, profile = tmp_path / 'home', tmp_path / 'profile'
    monkeypatch.setenv('HERMES_HOME', str(home))
    original_wait = launcher._wait_for_devtools
    launched = []

    def takeover_during_readiness(process, directory, deadline):
        found, error = original_wait(process, directory, deadline)
        assert found is not None, error
        launched.append((process.pid, launcher._record_path(launcher._launching[process.pid])))
        lease.acquire('test-viewer', profile_key=str(home))
        if failure == 'interrupted':
            raise KeyboardInterrupt()
        return None, 'readiness failed after takeover'

    monkeypatch.setattr(launcher, '_wait_for_devtools', takeover_during_readiness)
    try:
        def launch():
            return launcher.launch_chromium('client', executable=str(binary), user_data_dir=str(profile),
                                            ephemeral=False, headed=False, env=dict(os.environ), timeout=20)

        if failure == 'interrupted':
            with pytest.raises(KeyboardInterrupt):
                launch()
        else:
            browser, error = launch()
            assert browser is None and error
        pid, record = launched[0]
        assert launcher._pid_alive(pid)
        assert record.exists()
        assert launcher.reap_orphaned_browsers(homes=[str(home)]) == 0
        assert launcher._pid_alive(pid)
        lease.release('test-viewer', profile_key=str(home))
        assert launcher.reap_orphaned_browsers(homes=[str(home)]) == 1
        assert not launcher._pid_alive(pid)
        assert not record.exists()
    finally:
        for pid, _ in launched:
            kill_fake(pid, binary)
