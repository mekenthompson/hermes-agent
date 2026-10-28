"""Direct consented browser_exec launchers retain and revoke their copied profile."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import select

import pytest

from tests.tools.browser_launcher_fakes import kill_fake, write_fake_chromium
from tools import browser_launcher as launcher
from tools.bot_desktop import lease

pytestmark = [pytest.mark.platforms('linux'), pytest.mark.live_system_guard_bypass]
REPO = Path(__file__).resolve().parents[2]
CLIENT = textwrap.dedent('''\
    import atexit, os, sys
    from pathlib import Path
    from tools import browser_launcher as launcher, browser_session as session
    from tools import browser_use_cli as cli, browser_lightpanda as lightpanda
    from tools import browser_session_cloud as cloud, browser_session_cdp as cdp
    from tools import browser_session_real_profile as real_profile
    from tools.bot_desktop import lease
    from hermes_cli import browser_connect
    from tests.tools.browser_launcher_fakes import install_fake_chromium_identity
    binary, profile, mode = sys.argv[1:]
    install_fake_chromium_identity(Path(binary))
    cloud.use_real_profile = lambda: mode != 'revoke'
    lightpanda.using_lightpanda_engine = lambda: False
    browser_connect.detect_default_chromium = lambda: 'chrome'
    browser_connect.chromium_executable = lambda browser: binary
    browser_connect.real_profile_copy_dir = lambda browser: profile
    browser_connect.snapshot_real_profile = lambda browser: (profile, None)
    session.ensure_screen_for_headed_chromium = lambda: None
    cli._real_profile_consented = lambda: True
    cdp.get_cdp_override_raw = lambda: ''
    cloud.get_cloud_provider = lambda: None
    if mode == 'revoke':
        assert real_profile.real_profile_cdp() == (None, None)
    else:
        assert cli._resolve_real_profile_cdp({}, force_local=False) is None
        browser = launcher.get_browser(real_profile.real_profile_key())
        assert browser is not None and not browser.ephemeral
        assert not session._active_sessions
        if mode == 'human':
            lease.acquire('real-profile-viewer')
        atexit.register(session.detach_shared_browsers)
        print(browser.pid, flush=True)
        if mode == 'holder':
            sys.stdin.read()
''')


@pytest.mark.parametrize('human', [False, True])
def test_direct_real_profile_exit_retains_and_consent_off_respects_human(tmp_path, human):
    binary = write_fake_chromium(tmp_path / 'chromium')
    home = tmp_path / 'home'
    profile = home / 'browser-profile/chrome'

    def run(mode):
        return subprocess.run([sys.executable, '-c', CLIENT, str(binary), str(profile), mode],
                              cwd=REPO, env={**os.environ, 'HERMES_HOME': str(home)},
                              text=True, capture_output=True, timeout=45, check=True)

    first = run('human' if human else 'normal')
    pid = int(first.stdout.strip())
    try:
        assert launcher._pid_alive(pid), 'direct browser_exec route was missed by detach'
        launcher.reap_orphaned_browsers(homes=[str(home)])
        assert launcher._pid_alive(pid)
        run('revoke')
        if human:
            assert launcher._pid_alive(pid)
            assert profile.is_dir(), 'consent cleanup deleted a human-held browser profile'
            lease.release('real-profile-viewer', profile_key=str(home))
            run('revoke')
        assert not launcher._pid_alive(pid), 'consent withdrawal did not stop retained browser'
        assert not profile.parent.exists()
    finally:
        kill_fake(pid, binary)


def test_concurrent_holder_started_before_consent_cleanup_keeps_its_data(tmp_path, monkeypatch):
    from hermes_cli import browser_connect
    from tools import browser_session_cloud as cloud, browser_session_real_profile as real_profile
    from tests.tools.browser_launcher_fakes import install_fake_chromium_identity

    binary = write_fake_chromium(tmp_path / 'chromium')
    home, profile = tmp_path / 'home', tmp_path / 'home/browser-profile/chrome'
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(browser_connect, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(cloud, 'use_real_profile', lambda: False)
    monkeypatch.setattr(launcher, '_verified_local_chromium', launcher._verified_local_chromium)
    install_fake_chromium_identity(binary)
    original = real_profile.terminate_real_profile_browser
    child = None
    pid = None

    def start_after_stop():
        nonlocal child, pid
        original()
        child = subprocess.Popen([sys.executable, '-c', CLIENT, str(binary), str(profile), 'holder'],
                                 cwd=REPO, env={**os.environ, 'HERMES_HOME': str(home)},
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        assert select.select([child.stdout], [], [], 30)[0], 'concurrent launcher did not become ready'
        pid = int(child.stdout.readline().strip())
        (profile / 'active-work').write_text('fixture')

    monkeypatch.setattr(real_profile, 'terminate_real_profile_browser', start_after_stop)
    try:
        assert real_profile.real_profile_cdp() == (None, None)
        assert launcher._pid_alive(pid)
        assert (profile / 'active-work').read_text() == 'fixture'
        child.communicate(input='', timeout=20)
        assert child.returncode == 0
        monkeypatch.setattr(real_profile, 'terminate_real_profile_browser', original)
        assert real_profile.real_profile_cdp() == (None, None)
        assert not launcher._pid_alive(pid)
        assert not profile.exists()
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
        if pid is not None:
            kill_fake(pid, binary)


def test_snapshot_deletion_keeps_the_native_cross_process_launch_lock(tmp_path, monkeypatch):
    import shutil
    from hermes_cli import browser_connect

    home, profile = tmp_path / 'home', tmp_path / 'home/browser-profile/chrome'
    profile.mkdir(parents=True)
    (profile / 'copy').write_text('fixture')
    monkeypatch.setattr(browser_connect, 'get_hermes_home', lambda: home)
    original = shutil.rmtree
    locked = []

    def check_lock_then_remove(path, *args, **kwargs):
        if Path(path) == profile:
            state = launcher._shared_state_dir(str(profile))
            code = "from pathlib import Path; import sys; " \
                   "from tools.browser_launcher_holders import records_lock; " \
                   "\nwith records_lock(Path(sys.argv[1]),blocking=False) as held: print(held)"
            result = subprocess.run([sys.executable, '-c', code, str(state)], cwd=REPO,
                                    capture_output=True, text=True, timeout=20, check=True)
            assert result.stdout.strip() == 'False', 'deletion released the launch lock'
            locked.append(True)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(shutil, 'rmtree', check_lock_then_remove)
    browser_connect.cleanup_real_profile_snapshots()
    assert locked == [True]
    assert not profile.parent.exists()
