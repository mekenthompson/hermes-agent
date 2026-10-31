"""Profile-scoped required Chromium sandbox policy across canonical launchers."""
from hermes_cli.config import atomic_config_write
from tools import browser_launcher as launcher
from tools.bot_desktop import browser
import pytest


@pytest.mark.parametrize('flag', ['--no-sandbox', '--disable-setuid-sandbox', '--disable-seccomp-filter-sandbox', '--disable-namespace-sandbox', '--single-process'])
def test_required_sandbox_refuses_explicit_bypass(tmp_path, monkeypatch, flag):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    atomic_config_write(tmp_path / 'config.yaml', {'browser': {'sandbox_required': True}})
    monkeypatch.setattr(launcher, '_configured_chromium_args', lambda: [flag])
    with pytest.raises(ValueError, match='sandbox_required'):
        launcher.build_chromium_argv('/chromium', '/profile', packaged=True,
                                     headed=True, sandbox_bypass=True)



def test_browser_default_mapping_is_fresh_and_matches_config():
    from hermes_cli.config_defaults_browser import default_browser_config
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    first = default_browser_config()
    assert first == DEFAULT_CONFIG['browser']
    assert first['sandbox_required'] is False
    first['chromium_args'].append('--lang=de')
    assert default_browser_config()['chromium_args'] == []


def test_required_policy_returns_actionable_error_without_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    atomic_config_write(tmp_path / 'config.yaml', {'browser': {'sandbox_required': True}})
    monkeypatch.setattr(launcher, '_configured_chromium_args', list)
    def forbidden_spawn(*args):
        pytest.fail('must not spawn a browser with sandbox-disabling flags')
    monkeypatch.setattr(launcher, '_spawn', forbidden_spawn)
    launched, error = launcher.launch_chromium('required-policy', executable='/chromium',
        user_data_dir=str(tmp_path / 'profile'), extra_args=['--no-sandbox'], env={}, headed=False)
    assert launched is None
    assert 'sandbox_required' in error


def test_required_policy_rejects_existing_unsafe_browser_without_touching_it(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    atomic_config_write(tmp_path / 'config.yaml', {'browser': {'sandbox_required': True}})
    existing = SimpleNamespace(pid=123, foreign=False)
    monkeypatch.setattr(launcher, 'get_browser', lambda key: existing)
    monkeypatch.setattr('psutil.Process', lambda pid: SimpleNamespace(cmdline=lambda: ['/chromium', '--no-sandbox']))
    launched, error = launcher.launch_chromium('existing-policy', executable='/chromium', env={})
    assert launched is None
    assert error and 'sandbox_required' in error


def test_required_policy_does_not_adopt_unsafe_shared_holder(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    atomic_config_write(tmp_path / 'config.yaml', {'browser': {'sandbox_required': True}})
    monkeypatch.setattr(launcher, 'running_instance', lambda udd: (9222, '/browser', 123))
    monkeypatch.setattr('psutil.Process', lambda pid: SimpleNamespace(cmdline=lambda: ['/chromium', '--no-sandbox']))
    monkeypatch.setattr(launcher, '_make_browser', lambda *a, **kw: pytest.fail('unsafe browser must not be leased'))
    monkeypatch.setattr(launcher, '_live_local_holder', lambda udd: None)
    attached, error = launcher._attach_local('test', str(tmp_path), str(tmp_path), 0)
    assert attached is None
    assert error and 'sandbox_required' in error


def test_required_policy_refuses_foreign_holder_without_connecting(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    atomic_config_write(tmp_path / 'config.yaml', {'browser': {'sandbox_required': True}})
    monkeypatch.setattr(launcher, '_read_devtools_file', lambda udd: pytest.fail('foreign browser cannot be verified'))
    assert launcher._attach_foreign('test', str(tmp_path), str(tmp_path), 123) is None


def test_required_sandbox_reaches_agent_and_dock(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    atomic_config_write(tmp_path / 'config.yaml', {'browser': {'sandbox_required': True}})
    monkeypatch.setattr(launcher, 'running_in_docker', lambda: True)
    monkeypatch.setattr(launcher, '_configured_chromium_args', list)
    argv = launcher.build_chromium_argv('/chromium', '/profile', packaged=True,
                                        headed=True, sandbox_bypass=True)
    assert '--no-sandbox' not in argv
    assert '--no-sandbox' not in browser.dock_argv('/chromium', '/profile')
    assert '--no-sandbox' not in browser.dock_argv('/chromium', '/profile', sandbox_bypass=True)
