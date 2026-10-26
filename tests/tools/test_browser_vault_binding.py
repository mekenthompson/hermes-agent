"""Vault I/O follows the native provenance on its actual CDP connection."""
import json
from types import SimpleNamespace

import pytest

from tools import browser_launcher as launcher, browser_session as session
from tools import browser_supervisor, browser_use_cli, browser_vault_tool as vault
from tools.bot_desktop import lease, runtime

pytestmark = pytest.mark.platforms('linux')


@pytest.fixture
def bound_connection(tmp_path, monkeypatch):
    class Supervisor:
        def __init__(self, task_id, cdp_url, **kwargs):
            self.cdp_url = cdp_url
            self.browser_session_info = None
            self._thread = SimpleNamespace(is_alive=lambda: True)
            self._loop = SimpleNamespace(is_running=lambda: True)
            self.evaluated = []
            self.focused = []
            self.stopped = False

        def start(self, **kwargs):
            return None

        def stop(self):
            self.stopped = True

        def evaluate_runtime(self, expression):
            self.evaluated.append(expression)
            return {'ok': True, 'result': 42}

        def focus_page(self, origin, **kwargs):
            self.focused.append(origin)
            return {'ok': True, 'url': origin}

    owner, caller = tmp_path / 'owner', tmp_path / 'caller'
    monkeypatch.setenv('HERMES_HOME', str(caller))
    monkeypatch.setattr(browser_supervisor, 'CDPSupervisor', Supervisor)
    registry = browser_supervisor._SupervisorRegistry()
    monkeypatch.setattr(browser_supervisor, 'SUPERVISOR_REGISTRY', registry)
    monkeypatch.setattr(runtime, 'published_env', lambda: {'DISPLAY': ':37'})
    monkeypatch.setattr('tools.browser_session_cdp.get_dialog_policy_config', lambda: ('dismiss', 30))
    browser = launcher.LaunchedBrowser('native', 123, 456, 'ws://127.0.0.1:1/devtools/browser/fixture',
                                       'http://127.0.0.1:1', str(tmp_path / 'profile'), True, False, str(owner))
    launcher._write_record(browser)
    monkeypatch.setattr(launcher, 'get_browser', lambda key: browser)
    info = {'launcher_key': 'native', 'cdp_url': browser.cdp_url, 'features': {'local': True}}
    browser_use_cli._attach_vault_supervisor({'BU_CDP_WS': browser.cdp_url}, 'task', info)
    return registry, str(owner), str(caller), tmp_path


@pytest.mark.parametrize('named_session', [False, True])
def test_vault_binding_refuses_human_prompt_takeover_and_resumes(bound_connection, monkeypatch, named_session):
    registry, owner, caller, _root = bound_connection
    active = {'bu-named-helper': {'home_key': caller, 'features': {'local': True}}} if named_session else {}
    monkeypatch.setattr(session, '_active_sessions', active)
    supervisor = registry.get('task')
    lease.acquire('viewer', profile_key=owner)
    try:
        result = json.loads(vault._fenced_page_op('task', lambda: json.dumps(vault._eval_js_secret('task', '42'))))
        assert result.get('code') == 'human_has_control'
        assert vault._eval_js_secret('task', '42').get('error_type') == 'human_has_control'
        assert vault._eval_js('task', '42').get('error_type') == 'human_has_control'
        assert supervisor.evaluated == []
    finally:
        lease.release('viewer', profile_key=owner)

    def prompt_then_write():
        lease.acquire('viewer', profile_key=owner)
        return json.dumps(vault._eval_js_secret('task', '42'))

    try:
        result = json.loads(vault._fenced_page_op('task', prompt_then_write))
        assert result.get('code') == 'human_has_control'
        assert supervisor.evaluated == []
    finally:
        lease.release('viewer', profile_key=owner)
    assert vault._eval_js_secret('task', '42') == {'success': True, 'result': 42}


def test_captured_connection_keeps_its_owner_when_task_rebinds(bound_connection, monkeypatch):
    from tools.browser_session_fence import evaluate_vault

    registry, owner, caller, root = bound_connection
    original = registry.get('task')
    cloud = registry.get_or_start('task', 'ws://external', session_info={'features': {}})
    lease.acquire('viewer', profile_key=owner)
    try:
        assert evaluate_vault(original, 'task', '42').get('error_type') == 'human_has_control'
        assert original.evaluated == []
    finally:
        lease.release('viewer', profile_key=owner)
    monkeypatch.setenv('HERMES_HOME', str(root / 'different-caller'))
    result = json.loads(vault._fenced_page_op('task', lambda: 'must-not-run'))
    assert result.get('code') == 'browser_profile_mismatch'
    monkeypatch.setenv('HERMES_HOME', caller)
    local = registry.get_or_start('local', 'ws://native', session_info={
        'features': {'local': True}, 'shared_profile': True, 'display_home_key': owner})
    registry.stop_shared_home(caller)
    assert local.stopped and registry.get('local') is None
    assert registry.get('task') is cloud and not cloud.stopped


@pytest.mark.parametrize('cold_connection', [False, True])
def test_payment_confirmation_takeover_prevents_all_page_io(bound_connection, monkeypatch, cold_connection):
    registry, owner, _caller, _root = bound_connection
    supervisor = registry.get('task')
    if cold_connection:
        info = registry.browser_session('task')
        registry.stop('task')
        monkeypatch.setattr(session, '_active_sessions', {'task': info})
    meta = SimpleNamespace(kind='payment', label='Fixture', origin='https://example.test',
                           allowed_origins=['https://example.test'])
    backend = SimpleNamespace(needs_unlock=False, get_meta=lambda handle: meta)
    monkeypatch.setattr('agent.vault_backends.backend_for_handle', lambda handle: backend)

    def confirm(*args):
        lease.acquire('viewer', profile_key=owner)
        return True

    monkeypatch.setattr(vault, '_confirm_payment_fill', confirm)
    try:
        result = json.loads(vault._handle_vault({'action': 'fill', 'handle': 'fixture'}, task_id='task'))
        assert result.get('code') == 'human_has_control'
        assert supervisor.focused == []
        assert supervisor.evaluated == []
        if cold_connection:
            assert registry.get('task') is None, 'takeover started a new page connection'
    finally:
        lease.release('viewer', profile_key=owner)
