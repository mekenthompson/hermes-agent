"""Fresh native quick commands enforce Stop at spawn and reconcile actual running processes."""

import asyncio
import json
import os
import shlex
import signal
import threading
from types import SimpleNamespace

import psutil
import pytest

from tests.waiting import async_wait_until, wait_until


def _configure(home, commands):
    path = home / 'config.yaml'
    config = json.loads(path.read_text()) if path.exists() else {}
    config['quick_commands'] = {name: {'type': 'exec', 'command': command} for name, command in commands.items()}
    path.write_text(json.dumps(config))


def _invoke(wire, sid, command):
    result = {}

    def run():
        try:
            result['value'] = wire.call('slash.exec', session_id=sid, command=command)
        except Exception as exc:
            result['error'] = str(exc)
            result['code'] = getattr(exc, 'code', None)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, result


def _pid(path):
    return int(path.read_text().strip()) if path.exists() and path.read_text().strip() else None


def _alive(pid):
    if pid is None:
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def _native_session(tmp_path, monkeypatch, commands):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session

    home = tmp_path / 'home'
    home.mkdir()
    _configure(home, commands)
    monkeypatch.setenv('HERMES_HOME', str(home))
    runner = GatewayRunner(GatewayConfig(sessions_dir=home / 'sessions'))
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id='owner')
    actor = Principal('native-user', str(home), frozenset({'session:create', 'session:read', 'session:control'}), 'native')
    ref = create_local_session(authority, actor, {'request_id': 'fresh-quick', 'source': 'cli',
        'model': 'fixture', 'toolsets': [], 'cwd': str(tmp_path)})
    await authority.attach(actor, ref)
    assert authority._handle(ref).execution_state == 'idle'
    return SimpleNamespace(authority=authority, actor=actor, ref=ref,
                           connection=SimpleNamespace(authority=authority, actor=actor, subscriptions={}))


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('revocation', ['live', 'stop', 'replacement'])
async def test_native_quick_shell_rechecks_owner_after_cwd_validation(tmp_path, monkeypatch, revocation):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_commands import execute_command
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from hermes_state_runtime import begin_runtime_epoch
    import gateway.session_commands as commands

    home = tmp_path / 'home'
    home.mkdir()
    effect = tmp_path / 'after-cwd-wait'
    _configure(home, {'delayed-effect': 'printf current > ' + shlex.quote(str(effect))})
    monkeypatch.setenv('HERMES_HOME', str(home))
    runner = GatewayRunner(GatewayConfig(sessions_dir=home / 'sessions'))
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id='owner')
    actor = Principal('native-user', str(home), frozenset({'session:create', 'session:read', 'session:control'}), 'native')
    ref = create_local_session(authority, actor, {'request_id': 'fresh-quick', 'source': 'cli',
        'model': 'fixture', 'toolsets': [], 'cwd': str(tmp_path)})
    await authority.attach(actor, ref)
    assert authority._handle(ref).execution_state == 'idle'
    entered, release = threading.Event(), threading.Event()
    actual_isdir = commands.os.path.isdir

    def validate_folder(path):
        result = actual_isdir(path)
        if str(path) == str(tmp_path):
            entered.set()
            assert release.wait(15)
        return result

    monkeypatch.setattr(commands.os.path, 'isdir', validate_folder)
    connection = SimpleNamespace(authority=authority, actor=actor, subscriptions={})
    pending = asyncio.create_task(execute_command(connection, ref, {'command': '/delayed-effect'}))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        route = authority.sessions[ref.session_id].route
        arrival = authority.route_epoch(route)
        if revocation == 'stop':
            await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
            assert authority.route_epoch(route) > arrival
        elif revocation == 'replacement':
            begin_runtime_epoch(authority.db, instance_id='replacement')
        release.set()
        outcome, = await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 15)
        if revocation == 'live':
            assert not isinstance(outcome, BaseException) and outcome['type'] == 'exec', outcome
            assert effect.read_text() == 'current'
        else:
            assert not effect.exists(), outcome
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        authority.db.close()


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('stopped', [False, True])
def test_fresh_websocket_quick_command_respects_stop_after_cwd_wait(tmp_path, stopped):
    from tests.gateway.test_cli_plugin_command_in_gateway import _plugin_home, _Wire
    from tests.gateway.fixtures.local_recovery_probe import daemon

    effect, armed, entered, gate = [tmp_path / name for name in ('effect', 'armed', 'entered', 'gate')]
    os.mkfifo(gate)
    plugin = f'''from pathlib import Path
import gateway.session_commands as commands
_actual_isdir = commands.os.path.isdir

def delayed_isdir(path):
    result = _actual_isdir(path)
    armed = Path({str(armed)!r})
    if str(path) == {str(tmp_path)!r} and armed.exists():
        armed.unlink()
        Path({str(entered)!r}).touch()
        with open({str(gate)!r}) as release:
            release.read()
    return result

def register(ctx):
    commands.os.path.isdir = delayed_isdir
'''
    with _plugin_home(tmp_path, plugin) as (root, home, env):
        _configure(home, {'delayed-effect': 'printf current > ' + shlex.quote(str(effect))})
        with daemon(root, home, env, barrier=False) as (proc, desc):
            caller, controller = _Wire(home, desc), _Wire(home, desc)
            worker = None
            try:
                created = controller.call('session.create', request_id='fresh-wire-quick', source='cli',
                                           toolsets=[], cwd=str(tmp_path))
                sid = created['session_id']
                caller.call('session.resume', session_id=sid)
                armed.touch()
                worker, result = _invoke(caller, sid, '/delayed-effect')
                wait_until(entered.exists, timeout=20, what='fresh WebSocket command cwd wait')
                if stopped:
                    result['stop'] = controller.call('session.interrupt', session_id=sid,
                                                    execution_generation=created['execution_generation'])
                with gate.open('w') as release:
                    release.write('complete validated cwd read')
                worker.join(20)
                assert not worker.is_alive(), result
                if stopped:
                    assert not effect.exists(), result
                else:
                    assert 'error' not in result and effect.read_text() == 'current', result
            finally:
                if entered.exists():
                    # A failed Stop assertion must not leave the real cwd worker
                    # blocked in the disposable daemon's FIFO during shutdown.
                    try:
                        fd = os.open(gate, os.O_WRONLY | os.O_NONBLOCK)
                    except OSError:
                        pass
                    else:
                        try:
                            os.write(fd, b'cleanup validation wait')
                        finally:
                            os.close(fd)
                caller.close()
                controller.close()
                if worker is not None:
                    worker.join(1)
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=20)


def _waiting_shell(root):
    shell, child, entered, release, effect = [root / name for name in ('shell.pid', 'child.pid', 'entered', 'release', 'effect')]
    command = ('echo $$ > ' + shlex.quote(str(shell)) + '; sleep 120 & quick_child=$!; echo "$quick_child" > '
        + shlex.quote(str(child)) + '; touch ' + shlex.quote(str(entered)) + '; while [ ! -e '
        + shlex.quote(str(release)) + ' ]; do sleep 0.05; done; printf current > ' + shlex.quote(str(effect))
        + '; kill "$quick_child"; wait "$quick_child" 2>/dev/null || true')
    return SimpleNamespace(shell=shell, child=child, entered=entered, release=release, effect=effect, command=command)


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('stopped', [False, True])
def test_websocket_stop_reaps_its_actual_quick_shell_tree_and_preserves_other_conversation(tmp_path, stopped):
    from tests.gateway.test_cli_plugin_command_in_gateway import _plugin_home, _Wire
    from tests.gateway.fixtures.local_recovery_probe import daemon

    first, other = tmp_path / 'first', tmp_path / 'other'
    first.mkdir()
    other.mkdir()
    own, separate = _waiting_shell(first), _waiting_shell(other)
    with _plugin_home(tmp_path, 'def register(ctx):\n    pass\n') as (root, home, env):
        _configure(home, {'waiting-effect': own.command, 'other-effect': separate.command})
        with daemon(root, home, env, barrier=False) as (proc, desc):
            caller, neighbor, controller = _Wire(home, desc), _Wire(home, desc), _Wire(home, desc)
            workers = []
            try:
                created = controller.call('session.create', request_id='fresh-running-quick', source='cli',
                                           toolsets=[], cwd=str(tmp_path))
                other_created = controller.call('session.create', request_id='other-running-quick', source='cli',
                                                 toolsets=[], cwd=str(tmp_path))
                caller.call('session.resume', session_id=created['session_id'])
                neighbor.call('session.resume', session_id=other_created['session_id'])
                worker, result = _invoke(caller, created['session_id'], '/waiting-effect')
                workers.append(worker)
                second_worker, second_result = _invoke(neighbor, other_created['session_id'], '/other-effect')
                workers.append(second_worker)
                wait_until(lambda: own.entered.exists() and separate.entered.exists(), timeout=20,
                           what='both native quick shell trees running')
                shell, child = _pid(own.shell), _pid(own.child)
                other_shell, other_child = _pid(separate.shell), _pid(separate.child)
                assert _alive(shell) and _alive(child) and _alive(other_shell) and _alive(other_child)
                if stopped:
                    try:
                        result['stop'] = controller.call('session.interrupt', session_id=created['session_id'],
                                                        execution_generation=created['execution_generation'])
                    except Exception as exc:
                        raise AssertionError({'error': str(exc), 'shell_alive': _alive(shell),
                            'child_alive': _alive(child), 'daemon': (home / 'restart.log').read_text()}) from exc
                    assert not _alive(shell) and not _alive(child), ('Stop acknowledged without physical cleanup', result)
                    assert _alive(other_shell) and _alive(other_child), 'Stop crossed into another conversation'
                own.release.touch()
                separate.release.touch()
                for thread in workers:
                    thread.join(20)
                    assert not thread.is_alive(), (result, second_result)
                assert 'error' not in second_result and separate.effect.read_text() == 'current', second_result
                if stopped:
                    assert not own.effect.exists(), result
                else:
                    assert 'error' not in result and own.effect.read_text() == 'current', result
                wait_until(lambda: not any(_alive(pid) for pid in (shell, child, other_shell, other_child)),
                           timeout=10, what='all owned quick shell trees physically settled')
            finally:
                own.release.touch()
                separate.release.touch()
                for thread in workers:
                    thread.join(5)
                caller.close()
                neighbor.close()
                controller.close()
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=20)


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
@pytest.mark.parametrize('failure', ['cleanup_denied', 'stale_identity'])
async def test_native_stop_reports_unknown_when_quick_process_cleanup_cannot_be_proven(tmp_path, monkeypatch, failure):
    from gateway.session_commands import execute_command
    from gateway.session_runtime_workers import join_authority_work, uncounted_runtime_work
    from hermes_state_runtime import RuntimeStoreError
    from tools import process_registry

    tree = _waiting_shell(tmp_path)
    runtime = await _native_session(tmp_path, monkeypatch, {'waiting-effect': tree.command})
    registry = process_registry.ProcessRegistry()
    monkeypatch.setattr(process_registry, 'process_registry', registry)
    pending = asyncio.create_task(execute_command(runtime.connection, runtime.ref, {'command': '/waiting-effect'}))
    try:
        await async_wait_until(tree.entered.exists, timeout=15, what='native quick process before cleanup failure')
        session, = registry._running.values()
        shell, child = _pid(tree.shell), _pid(tree.child)
        await async_wait_until(lambda: session.pid == shell, timeout=10, what='actual target birth receipt')
        assert _alive(shell) and _alive(child) and _alive(session.custody_pid)
        assert session.host_start_time is not None and session.custody_start_time is not None
        actual_start = registry._safe_host_start_time

        def deny_cleanup(*_args, **_kwargs):
            raise PermissionError('test process signals denied')

        def changed_identity(pid):
            value = actual_start(pid)
            return value + 1 if pid == session.custody_pid and value is not None else value

        with pytest.MonkeyPatch.context() as patch:
            if failure == 'cleanup_denied':
                patch.setattr(session._custody, 'stop', deny_cleanup)
            else:
                patch.setattr(process_registry.ProcessRegistry, '_safe_host_start_time', staticmethod(changed_identity))
            with pytest.raises(RuntimeStoreError) as refused:
                await runtime.authority.interrupt(runtime.actor, runtime.ref,
                                                   runtime.authority._handle(runtime.ref).execution_generation)
            assert refused.value.reason == 'unknown_execution'
            assert _alive(shell) and _alive(child), 'unconfirmed identity must not signal a replacement process'
            outcome = await asyncio.wait_for(pending, 15)
            assert 'reconcile' in outcome['output'].lower() or 'unconfirmed' in outcome['output'].lower(), outcome
            retained = list(registry._running.values())
            assert len(retained) == 1 and retained[0].pid == shell
            assert retained[0].reconciliation_required and not retained[0].exited
            checkpoint = json.loads(process_registry._checkpoint_path().read_text())
            assert any(row['session_id'] == retained[0].id and row['pid'] == shell
                       and row['custody_pid'] == session.custody_pid
                       and row['custody_start_time'] == session.custody_start_time
                       and row['reconciliation_required'] for row in checkpoint)
            assert not tree.effect.exists()
            assert uncounted_runtime_work(runtime.authority.runner) > 0
            with pytest.raises((TimeoutError, RuntimeStoreError)):
                await join_authority_work(runtime.authority, timeout=2)
            assert _alive(shell) and _alive(child), 'profile retirement cannot claim unconfirmed cleanup succeeded'
    finally:
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')
        tree.release.touch()
        await asyncio.gather(pending, return_exceptions=True)
        runtime.authority.db.close()


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
async def test_profile_retirement_joins_the_actual_owned_quick_shell_tree(tmp_path, monkeypatch):
    from gateway.session_commands import execute_command
    from gateway.session_runtime_workers import (command_effects, join_authority_work,
                                                stop_authority_work, uncounted_runtime_work)
    from tools import process_registry

    tree = _waiting_shell(tmp_path)
    runtime = await _native_session(tmp_path, monkeypatch, {'waiting-effect': tree.command})
    registry = process_registry.ProcessRegistry()
    monkeypatch.setattr(process_registry, 'process_registry', registry)
    pending = asyncio.create_task(execute_command(runtime.connection, runtime.ref, {'command': '/waiting-effect'}))
    try:
        await async_wait_until(tree.entered.exists, timeout=15, what='profile-owned quick shell tree')
        shell, child = _pid(tree.shell), _pid(tree.child)
        assert _alive(shell) and _alive(child)
        assert command_effects(runtime.authority) and uncounted_runtime_work(runtime.authority.runner) > 0
        stop_authority_work(runtime.authority)
        await join_authority_work(runtime.authority, timeout=15)
        assert not _alive(shell) and not _alive(child), 'native profile ownership cannot release a running tree'
        tree.release.touch()
        await asyncio.wait_for(pending, 15)
        assert not tree.effect.exists() and not command_effects(runtime.authority)
    finally:
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')
        tree.release.touch()
        await asyncio.gather(pending, return_exceptions=True)
        runtime.authority.db.close()


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
@pytest.mark.parametrize('stopped', [False, True, pytest.param('repeated_cancel', id='repeated_observer_cancel')])
async def test_native_stop_reconciles_actual_spawn_before_its_receipt_returns(tmp_path, monkeypatch, stopped):
    from gateway.session_commands import execute_command
    from gateway.session_runtime_workers import command_effects, join_authority_work, uncounted_runtime_work
    from tools import process_registry
    from tools.process_registry_custody import LocalCustody

    tree = _waiting_shell(tmp_path)
    runtime = await _native_session(tmp_path, monkeypatch, {'waiting-effect': tree.command})
    registry = process_registry.ProcessRegistry()
    monkeypatch.setattr(process_registry, 'process_registry', registry)
    entered, receipt = threading.Event(), threading.Event()
    actual_wait, sessions = LocalCustody.wait_event, []

    def wait_for_target_then_hold(custody, event, timeout=15):
        result = actual_wait(custody, event, timeout)
        if event is custody.started:
            # Hold the producer's actual target receipt, leaving the independent
            # control receiver free to collect whole-scope Stop evidence.
            sessions.append(custody.session)
            entered.set()
            assert receipt.wait(20)
        return result

    monkeypatch.setattr(LocalCustody, 'wait_event', wait_for_target_then_hold)
    pending = asyncio.create_task(execute_command(runtime.connection, runtime.ref, {'command': '/waiting-effect'}))
    stopping = None
    try:
        assert await asyncio.to_thread(entered.wait, 15)
        await async_wait_until(tree.entered.exists, timeout=15, what='real shell launched before receipt')
        session, = sessions
        shell, child = _pid(tree.shell), _pid(tree.child)
        assert session.pid == shell and _alive(shell) and _alive(child)
        if stopped:
            route = runtime.authority.sessions[runtime.ref.session_id].route
            arrival = runtime.authority.route_epoch(route)
            stopping = asyncio.create_task(runtime.authority.interrupt(runtime.actor, runtime.ref,
                runtime.authority._handle(runtime.ref).execution_generation))
            await async_wait_until(lambda: runtime.authority.route_epoch(route) > arrival,
                                   timeout=10, what='Stop while OS spawn receipt waits')
            if stopped == 'repeated_cancel':
                pending.cancel()
                cancelled_once = asyncio.Event()
                asyncio.get_running_loop().call_soon(cancelled_once.set)
                await cancelled_once.wait()
                pending.cancel()
                await async_wait_until(pending.done, timeout=10, what='cancelled quick-command observer')
                assert pending.cancelled() and not stopping.done()
                assert command_effects(runtime.authority) and uncounted_runtime_work(runtime.authority.runner) > 0
                assert _alive(shell) and _alive(child), 'the actual receipt must remain owned while it waits'
        receipt.set()
        if stopping is not None:
            await asyncio.wait_for(stopping, 20)
            assert not _alive(shell) and not _alive(child), 'Stop must settle the actual late process receipt'
        tree.release.touch()
        if stopped == 'repeated_cancel':
            outcome, = await asyncio.gather(pending, return_exceptions=True)
            assert isinstance(outcome, asyncio.CancelledError)
            await join_authority_work(runtime.authority, timeout=10)
            assert not command_effects(runtime.authority) and not uncounted_runtime_work(runtime.authority.runner)
            assert runtime.authority.sessions[runtime.ref.session_id].route == route
        else:
            outcome = await asyncio.wait_for(pending, 20)
        if stopped:
            assert not tree.effect.exists(), outcome
        else:
            assert tree.effect.read_text() == 'current'
    finally:
        receipt.set()
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')
        tree.release.touch()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        await asyncio.gather(pending, return_exceptions=True)
        runtime.authority.db.close()


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
async def test_native_stop_settles_custodied_descendant_after_actual_root_exit(tmp_path, monkeypatch):
    from gateway.session_commands import execute_command
    from gateway.session_runtime_workers import command_effects, join_authority_work, uncounted_runtime_work
    from tools import process_registry

    shell_file, child_file, entered, exit_root, release, effect = [tmp_path / name for name in (
        'shell.pid', 'child.pid', 'child-entered', 'exit-root', 'release-child', 'child-effect')]
    command = ('echo $$ > ' + shlex.quote(str(shell_file)) + '; (touch ' + shlex.quote(str(entered))
        + '; while [ ! -e ' + shlex.quote(str(release)) + ' ]; do sleep 0.05; done; printf child > '
        + shlex.quote(str(effect)) + ') & echo $! > ' + shlex.quote(str(child_file))
        + '; while [ ! -e ' + shlex.quote(str(exit_root)) + ' ]; do sleep 0.05; done; exit 0')
    runtime = await _native_session(tmp_path, monkeypatch, {'orphan-effect': command})
    registry = process_registry.ProcessRegistry()
    monkeypatch.setattr(process_registry, 'process_registry', registry)
    pending = asyncio.create_task(execute_command(runtime.connection, runtime.ref, {'command': '/orphan-effect'}))
    child = None
    try:
        await async_wait_until(lambda: entered.exists() and _pid(child_file), timeout=15,
                               what='actual quick descendant retaining the output pipe')
        session, = registry._running.values()
        shell, child = _pid(shell_file), _pid(child_file)
        await async_wait_until(lambda: session.pid == shell, timeout=10, what='actual root birth receipt')
        birth, custody = session.host_start_time, session._custody
        assert birth is not None and _alive(shell) and _alive(child)
        exit_root.touch()
        await async_wait_until(custody.main_exited.is_set, timeout=10, what='actual shell root exit receipt')
        assert custody.main_code == 0 and custody.main_remaining
        assert not _alive(shell) and _alive(child) and not pending.done()
        assert not custody.settled() and not session.exited
        checkpoint = json.loads(process_registry._checkpoint_path().read_text())
        assert any(row['session_id'] == session.id and row['pid'] == shell and row['host_start_time'] == birth
                   and row['custody_pid'] == session.custody_pid
                   and not row['custody_scope_empty'] for row in checkpoint)
        assert uncounted_runtime_work(runtime.authority.runner) > 0
        await runtime.authority.interrupt(runtime.actor, runtime.ref,
                                          runtime.authority._handle(runtime.ref).execution_generation)
        assert not _alive(child) and not _alive(session.custody_pid) and not effect.exists()
        assert custody.empty.is_set() and custody.settled() and session.custody_scope_empty
        assert session.exited and not session.reconciliation_required
        await asyncio.wait_for(pending, 15)
        await join_authority_work(runtime.authority, timeout=10)
        assert not command_effects(runtime.authority) and not uncounted_runtime_work(runtime.authority.runner)
        checkpoint = json.loads(process_registry._checkpoint_path().read_text())
        assert not any(row['session_id'] == session.id for row in checkpoint)
    finally:
        exit_root.touch()
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')
        release.touch()
        if child is not None:
            await async_wait_until(lambda: not _alive(child), timeout=10, what='released test descendant exit')
        await asyncio.gather(pending, return_exceptions=True)
        runtime.authority.db.close()
