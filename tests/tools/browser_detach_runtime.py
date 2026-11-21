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


def payment_prompt_check():
    from types import SimpleNamespace
    from agent import vault_backends
    from tools import browser_vault_tool as vault
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    from tools.bot_desktop import lease

    supervisor = SUPERVISOR_REGISTRY.get('default')
    original_backend, original_confirm = vault_backends.backend_for_handle, vault._confirm_payment_fill
    original_focus = supervisor.focus_page
    original_origin = vault._current_page_origin
    focused = []
    meta = SimpleNamespace(kind='payment', label='Fixture', origin='https://example.test',
                           allowed_origins=['https://example.test'])

    def focus(*args, **kwargs):
        focused.append(True)
        return original_focus(*args, **kwargs)

    def confirm(*args):
        focused.clear()  # pre-prompt origin selection is allowed; post-takeover I/O is not
        lease.acquire('payment-fixture-viewer')
        return True

    vault_backends.backend_for_handle = lambda handle: SimpleNamespace(needs_unlock=False, get_meta=lambda item: meta)
    vault._confirm_payment_fill, supervisor.focus_page = confirm, focus
    # The native browser starts on about:blank. Supply only the saved-origin fixture;
    # real supervisor operations and the human lease admission remain exercised.
    vault._current_page_origin = lambda task_id: meta.origin
    try:
        result = json.loads(vault._handle_vault({'action': 'fill', 'handle': 'fixture'}, task_id='default'))
        assert result.get('code') == 'human_has_control', result
        assert not focused, 'payment confirmation takeover dispatched page I/O'
        return True
    finally:
        vault_backends.backend_for_handle, vault._confirm_payment_fill = original_backend, original_confirm
        supervisor.focus_page = original_focus
        vault._current_page_origin = original_origin
        lease.release('payment-fixture-viewer')


def vault_binding_checks():
    from tools import browser_vault_tool as vault
    from tools.bot_desktop import lease

    task, viewer = 'default', 'vault-acceptance-viewer'
    lease.acquire(viewer)
    try:
        refused = json.loads(vault._fenced_page_op(task, lambda: json.dumps(vault._eval_js_secret(task, '42'))))
        assert refused.get('code') == 'human_has_control', refused
        assert vault._eval_js_secret(task, '42').get('error_type') == 'human_has_control'
    finally:
        lease.release(viewer)

    def prompt_then_write():
        lease.acquire(viewer)
        return json.dumps(vault._eval_js_secret(task, 'window.__vault_acceptance=42'))

    try:
        refused = json.loads(vault._fenced_page_op(task, prompt_then_write))
        assert refused.get('error_type') == 'human_has_control' or refused.get('code') == 'human_has_control', refused
    finally:
        lease.release(viewer)
    resumed = json.loads(vault._fenced_page_op(task, lambda: json.dumps(vault._eval_js_secret(task, '42'))))
    assert resumed.get('success') is True and resumed.get('result') == 42, resumed
    return {'vault_refusal': True, 'vault_prompt_takeover': True, 'vault_handback': True,
            'vault_payment_takeover': payment_prompt_check()}


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
        created = json.loads(result['output'])['targetId']
        info = browser_session.get_session_info('bu-named-detach-acceptance')
        return {'ok': True, 'pid': browser_launcher.get_browser(info['launcher_key']).pid, 'created': created}

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
    proof = vault_binding_checks() if mode == 'normal' else {}
    return {'ok': True, 'pid': browser.pid, 'http_url': browser.http_url, **targets, **proof}


def real_profile_client(action):
    from hermes_constants import get_hermes_home, hermes_home_key
    from hermes_cli import browser_connect
    from tools import browser_launcher, browser_session, browser_session_cloud, browser_use_cli
    from tools import browser_session_real_profile

    # An empty disposable copy stands in for the user's default profile. No
    # default-browser discovery or credential snapshot is permitted in this test.
    profile = str(get_hermes_home() / 'browser-profile/chrome')
    binary = browser_launcher._resolve_executable()
    browser_connect.detect_default_chromium = lambda: 'chrome'
    browser_connect.chromium_executable = lambda name: binary
    browser_connect.real_profile_copy_dir = lambda name: profile
    browser_connect.snapshot_real_profile = lambda name: (profile, None)
    # The Docker fixture has no user namespace sandbox. This empty, offline
    # test profile uses the packaged browser's container flags, never secrets.
    browser_session_real_profile._REAL_PROFILE_EXTRA_FLAGS = (
        *browser_session_real_profile._REAL_PROFILE_EXTRA_FLAGS, '--no-sandbox')
    browser_session_cloud.use_real_profile = lambda: action != 'real-revoke'
    browser_use_cli._real_profile_consented = lambda: True
    if action == 'real-revoke':
        assert browser_session_real_profile.real_profile_cdp() == (None, None)
        return {'ok': True}
    atexit.register(browser_session.detach_shared_browsers, hermes_home_key())
    result = browser_use_cli.browser_exec(
        code="import json\nprint(json.dumps(cdp('Target.createTarget',url='about:blank')['targetId']))",
        session='direct-real-profile', timeout_s=40)
    result = json.loads(result) if isinstance(result, str) else result
    assert result.get('success') is True, result
    assert not browser_session._active_sessions
    browser = browser_launcher.get_browser(browser_session_real_profile.real_profile_key())
    return {'pid': browser.pid, 'http_url': browser.http_url, 'target': json.loads(result['output']),
            **vault_binding_checks()}


def check_real_profile(root, checks):
    from tools import browser_launcher

    home = root / 'real-profile'
    home.mkdir(mode=0o700)
    (home / 'config.yaml').write_text(json.dumps({
        'browser': {'cloud_provider': 'local', 'headed': False, 'use_gateway': False},
        'bot_desktop': {'auto_start': False}}))

    def run(action):
        result = subprocess.run([sys.executable, __file__, 'client', action],
                                env={**os.environ, 'HERMES_HOME': str(home)},
                                capture_output=True, text=True, timeout=100, check=False)
        assert result.returncode == 0, result.stderr + result.stdout
        return json.loads(result.stdout.strip().splitlines()[-1])

    first = None
    try:
        first = run('real-open')
        checks.update({'direct_real_profile_' + key: first[key] for key in
                       ['vault_refusal', 'vault_prompt_takeover', 'vault_handback', 'vault_payment_takeover']})
        browser_launcher.reap_orphaned_browsers(homes=[str(home)])
        with urllib.request.urlopen(first['http_url'] + '/json/list', timeout=5) as response:
            assert first['target'] in {target['id'] for target in json.load(response)}
        checks['direct_real_profile_exit'] = True
        later = run('real-open')
        assert later['pid'] == first['pid']
        with urllib.request.urlopen(first['http_url'] + '/json/list', timeout=5) as response:
            assert {first['target'], later['target']} <= {target['id'] for target in json.load(response)}
        checks['direct_real_profile_later_client'] = True
        run('real-revoke')
        assert not browser_launcher._pid_alive(first['pid'])
        assert not (home / 'browser-profile').exists()
        checks['direct_real_profile_consent_off'] = True
    finally:
        if first is not None and browser_launcher._pid_alive(first['pid']):
            run('real-revoke')


def main():
    if len(sys.argv) == 3 and sys.argv[1] == 'client':
        action = sys.argv[2]
        result = real_profile_client(action) if action.startswith('real-') else client(action)
        print(json.dumps(result), flush=True)
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
                if mode == 'normal':
                    checks.update({key: first[key] for key in
                                   ['vault_refusal', 'vault_prompt_takeover', 'vault_handback', 'vault_payment_takeover']})
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
                    assert {first['owned'], first['unrelated']} <= target_ids(first['http_url'])
                    checks['cross_home_human_refusal'] = True
                    run('handback')
                    ordinary = run('ordinary', attacher)
                    assert ordinary['pid'] == first['pid']
                    assert {first['owned'], first['unrelated'], ordinary['created']} <= target_ids(first['http_url'])
                    checks['cross_home_handback'] = True
                run('explicit-stop')
                assert not browser_launcher._pid_alive(first['pid'])
                checks[mode + '_explicit_stop'] = True
            finally:
                # Only the test browser's verified profile is in this environment.
                if first is not None and browser_launcher._pid_alive(first['pid']):
                    run('handback')
                    run('explicit-stop')
        check_real_profile(root, checks)
        print(json.dumps({'ok': all(checks.values()), 'checks': checks}))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
