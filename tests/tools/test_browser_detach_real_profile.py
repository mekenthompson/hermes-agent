"""Direct consented browser_exec launchers retain and revoke their copied profile."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

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
    binary, profile, mode = sys.argv[1:]
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
