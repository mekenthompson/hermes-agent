"""A shared Chromium's display lease remains its launch home's after handover."""
from dataclasses import replace

import pytest

from hermes_constants import hermes_home_key
from tools import browser_launcher as launcher, browser_session as session
from tools.bot_desktop import lease, runtime


@pytest.fixture
def cross_home(tmp_path, monkeypatch):
    owner, attacher = tmp_path / 'owner', tmp_path / 'attacher'
    browser = launcher.LaunchedBrowser('shared', 123, 456, 'ws://test', 'http://test',
                                       str(tmp_path / 'chromium'), True, False, str(owner))
    launcher._write_record(browser)
    browser = replace(browser, owned=False, home_key=str(attacher))
    monkeypatch.setattr(launcher, 'get_browser', lambda key: browser)
    monkeypatch.setenv('HERMES_HOME', str(attacher))
    monkeypatch.setattr(runtime, 'published_env',
                        lambda: {'DISPLAY': ':37'} if hermes_home_key() == str(owner) else {})
    info = {'launcher_key': 'shared', 'cdp_url': browser.cdp_url,
            'home_key': str(attacher), 'features': {'local': True}}
    return info, str(owner), str(attacher)


@pytest.mark.platforms('linux')
def test_shared_attacher_refuses_owner_human_and_resumes_after_handback(cross_home):
    info, owner, attacher = cross_home
    lease.acquire('owner-viewer', profile_key=owner)
    dispatched = []
    assert session.human_holds_shared_browser(info)
    result = session.run_fenced(info, lambda: dispatched.append(True) or {'success': True})
    assert result.get('code') == 'human_has_control'
    assert dispatched == []
    assert hermes_home_key() == attacher
    lease.release('owner-viewer', profile_key=owner)
    assert session.run_fenced(info, lambda: {'success': True}) == {'success': True}


@pytest.mark.platforms('linux')
def test_shared_attacher_discards_result_crossing_owner_takeover(cross_home):
    info, owner, attacher = cross_home

    def take_over():
        assert hermes_home_key() == attacher
        lease.acquire('owner-viewer', profile_key=owner)
        lease.release('owner-viewer', profile_key=owner)
        return {'success': True, 'data': 'private-frame'}

    result = session.run_fenced(info, take_over)
    assert result.get('code') == 'human_has_control'
    assert 'data' not in result


@pytest.mark.platforms('linux')
def test_vault_readmission_uses_browser_owner_after_prompt(cross_home, monkeypatch):
    from tools import browser_vault_tool as vault

    info, owner, _attacher = cross_home
    monkeypatch.setattr(session, 'session_for_task', lambda task: info)
    evaluated = []

    class Supervisor:
        browser_session_info = None
        def evaluate_runtime(self, expression):
            evaluated.append(expression)
            return {'ok': True, 'result': '{}'}

    monkeypatch.setattr(vault, '_ensure_supervisor', lambda task: Supervisor())
    lease.acquire('owner-viewer', profile_key=owner)
    result = vault._eval_js_secret('task', 'fill()')
    assert result.get('error_type') == 'human_has_control'
    assert evaluated == []
