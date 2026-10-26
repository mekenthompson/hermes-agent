"""Real Chromium/CDP acceptance for native short-lived browser clients.

Run in a disposable environment with Chromium and browser-harness installed.
Exit 77 means the optional browser packages are unavailable; image acceptance
must require exit 0. All profiles, targets and cookies belong to this test.
"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request


def client(mode):
    from hermes_constants import hermes_home_key
    from tools import browser_launcher, browser_session
    from tools.bot_desktop import lease
    from tools.browser_use_cli import browser_exec

    def execute(code):
        result = browser_exec(code='import json\n' + code, session='detach-acceptance', timeout_s=40)
        return json.loads(result) if isinstance(result, str) else result

    if mode == 'handback':
        lease.release('acceptance-viewer')
        return {'ok': True}
    if mode == 'explicit-stop':
        info = browser_session.get_session_info('bu-named-detach-acceptance')
        browser_launcher.stop_browser(info['launcher_key'], force=True)
        return {'ok': True}
    if mode == 'refused':
        for command in ["cdp('Target.getTargets')", "cdp('Target.createTarget',url='about:blank')"]:
            result = execute(f"print(json.dumps({command}))")
            assert result.get('code') == 'human_has_control', result
        info = browser_session.get_session_info('bu-named-detach-acceptance')
        return {'ok': True, 'pid': browser_launcher.get_browser(info['launcher_key']).pid}
    if mode == 'ordinary':
        result = execute("print(json.dumps(cdp('Target.getTargets')))")
        assert result.get('success') is True, result
        result = execute("print(json.dumps(cdp('Target.createTarget',url='about:blank')))")
        assert result.get('success') is True, result
        info = browser_session.get_session_info('bu-named-detach-acceptance')
        return {'ok': True, 'pid': browser_launcher.get_browser(info['launcher_key']).pid}

    # Match the standalone client's LIFO shutdown registration, rather than
    # invoking detachment manually before the interpreter exits.
    atexit.register(browser_session.detach_shared_browsers, hermes_home_key())

    result = execute(
        "owned=cdp('Target.createTarget',url='about:blank')['targetId']\n"
        "other=cdp('Target.createTarget',url='about:blank')['targetId']\n"
        "print(json.dumps({'owned':owned,'unrelated':other}))")
    assert result.get('success') is True, result
    targets = json.loads(result['output'])
    info = browser_session.get_session_info('bu-named-detach-acceptance')
    browser = browser_launcher.get_browser(info['launcher_key'])
    assert browser is not None and not browser.ephemeral
    if mode == 'human':
        lease.acquire('acceptance-viewer')
    result = execute(f"close_tab({targets['owned']!r})\nprint('true')")
    assert result.get('code') == 'human_has_control' if mode == 'human' else result.get('success') is True
    return {'ok': True, 'pid': browser.pid, 'http_url': browser.http_url, **targets}


def main():
    if len(sys.argv) == 3 and sys.argv[1] == 'client':
        print(json.dumps(client(sys.argv[2])), flush=True)
        return 0
    with tempfile.TemporaryDirectory(prefix='native-browser-detach-') as directory:
        root = Path(directory)
        os.environ['HERMES_HOME'] = str(root / 'parent')
        from tools import browser_launcher
        from tools.browser_use_cli import browser_exec_available

        if not browser_exec_available() or not browser_launcher._resolve_executable():
            print(json.dumps({'skipped': 'Chromium and browser-harness are required'}))
            return 77
        checks = {}
        for mode in ['normal', 'human']:
            home = root / mode
            home.mkdir(mode=0o700)
            config = {'browser': {'cloud_provider': 'local', 'headed': False,
                                  'use_real_profile': False, 'use_gateway': False,
                                  'user_data_dir': str(home / 'chromium')},
                      'bot_desktop': {'auto_start': False}}
            (home / 'config.yaml').write_text(json.dumps(config))
            env = {**os.environ, 'HERMES_HOME': str(home)}

            def run(action, client_home=home):
                result = subprocess.run([sys.executable, __file__, 'client', action],
                                        env={**env, 'HERMES_HOME': str(client_home)}, capture_output=True, text=True,
                                        timeout=100, check=False)
                assert result.returncode == 0, result.stderr + result.stdout
                return json.loads(result.stdout.strip().splitlines()[-1])

            def target_ids(url):
                with urllib.request.urlopen(url + '/json/list', timeout=5) as response:
                    return {target['id'] for target in json.load(response)}

            first = None
            try:
                first = run(mode)
                browser_launcher.reap_orphaned_browsers(homes=[str(home)])
                ids = target_ids(first['http_url'])
                assert first['unrelated'] in ids
                assert (first['owned'] in ids) == (mode == 'human')
                checks[mode + '_exit_targets'] = True
                run('refused' if mode == 'human' else 'ordinary')
                assert first['unrelated'] in target_ids(first['http_url'])
                checks[mode + '_later_client'] = True
                if mode == 'human':
                    attacher = root / 'attacher'
                    attacher.mkdir(mode=0o700)
                    (attacher / 'config.yaml').write_text(json.dumps(config))
                    assert run('refused', attacher)['pid'] == first['pid']
                    assert target_ids(first['http_url']) == ids
                    checks['cross_home_human_refusal'] = True
                    run('handback')
                    assert run('ordinary', attacher)['pid'] == first['pid']
                    assert target_ids(first['http_url']) > ids
                    checks['cross_home_handback'] = True
                run('explicit-stop')
                assert not browser_launcher._pid_alive(first['pid'])
                checks[mode + '_explicit_stop'] = True
            finally:
                # Only the test browser's verified profile is in this environment.
                if first is not None and browser_launcher._pid_alive(first['pid']):
                    run('handback')
                    run('explicit-stop')
        print(json.dumps({'ok': all(checks.values()), 'checks': checks}))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
