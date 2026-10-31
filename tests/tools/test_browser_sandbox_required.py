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



@pytest.mark.platforms('posix')
@pytest.mark.parametrize('required', [True, False])
def test_startup_sandbox_failure_guidance_respects_required_policy(tmp_path, monkeypatch, required):
    import os
    import sys
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    atomic_config_write(tmp_path / 'config.yaml', {'browser': {'sandbox_required': required}})
    executable = tmp_path / 'chromium'
    executable.write_text(
        f'#!{sys.executable}\n'
        'import sys\n'
        'sys.stderr.write("No usable sandbox! Try using --no-sandbox.\\n")\n'
        'sys.exit(1)\n', encoding='utf-8'
    )
    executable.chmod(0o755)
    launched, error = launcher.launch_chromium(
        'startup-failure', executable=str(executable),
        user_data_dir=str(tmp_path / 'browser-profile'), headed=False, env=dict(os.environ))
    assert launched is None
    assert error is not None
    assert 'exited with code 1' in error
    if required:
        assert 'reviewed container sandbox policy' in error
        assert 'browser.sandbox_required: true' in error
        assert '--no-sandbox' not in error
    else:
        assert 'add --no-sandbox to browser.chromium_args' in error
        assert 'No usable sandbox!' in error


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


def test_required_policy_refuses_clean_argv_without_runtime_proof(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    atomic_config_write(tmp_path / 'config.yaml', {'browser': {'sandbox_required': True}})
    existing = SimpleNamespace(pid=123, foreign=False)
    monkeypatch.setattr(launcher, 'get_browser', lambda key: existing)
    monkeypatch.setattr('psutil.Process', lambda pid: SimpleNamespace(
        pid=pid, cmdline=lambda: ['/chromium'], children=lambda **kw: []))
    launched, error = launcher.launch_chromium('unproven-policy', executable='/chromium', env={})
    assert launched is None
    assert error and 'sandbox_required' in error


@pytest.mark.parametrize('changed,value', [
    (None, None), ('NoNewPrivs', '0'), ('Seccomp', '0'),
    ('Seccomp_filters', '1'), ('Seccomp_filters', None),
    ('CapEff', '1'), ('NSpid', '124'), ('Uid', '0 0 0 0'),
    ('missing_status', None), ('missing_renderer', None), ('exited', None),
])
def test_required_reuse_needs_kernel_renderer_evidence(tmp_path, monkeypatch, changed, value):
    from types import SimpleNamespace
    from hermes_cli import browser_runtime as runtime
    monkeypatch.setattr(runtime, 'chromium_sandbox_required', lambda: True)
    parent = {'Uid': '1000 1000 1000 1000', 'NSpid': '123', 'Seccomp_filters': '1'}
    renderer = dict(parent, NoNewPrivs='1', Seccomp='2', Seccomp_filters='2',
                    CapEff='0000000000000000', NSpid='124 2')
    if changed in renderer:
        if value is None:
            renderer.pop(changed)
        else:
            renderer[changed] = value
    for pid, fields in [(123, parent), (124, renderer)]:
        target = tmp_path / 'proc' / str(pid) / 'status'
        target.parent.mkdir(parents=True)
        target.write_text(''.join(f'{key}:\t{val}\n' for key, val in fields.items()), encoding='utf-8')
    if changed == 'missing_status':
        (tmp_path / 'proc' / '124' / 'status').unlink()
    monkeypatch.setattr(runtime, 'Path', lambda path: tmp_path / path.lstrip('/'))
    child = SimpleNamespace(pid=124, cmdline=lambda: ['/chromium --type=renderer'],
                            is_running=lambda: changed != 'exited')
    process = SimpleNamespace(pid=123, cmdline=lambda: ['/chromium'],
        children=lambda **kw: [] if changed == 'missing_renderer' else [child],
        is_running=lambda: True)
    monkeypatch.setattr('psutil.Process', lambda pid: process)
    error = runtime.chromium_existing_sandbox_error(123)
    assert (error is None) == (changed is None)


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
