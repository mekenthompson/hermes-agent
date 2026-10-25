"""Actual terminal launches stay owned across OS receipts and registry publication."""

import asyncio
from contextlib import closing
import json
import signal
import sqlite3
import threading

import pytest

from tests.gateway.test_native_terminal_stop_authority import _alive, _cleanup, _runtime, _start, _tree
from tests.waiting import async_wait_until, wait_until


async def _submit(runtime, tree, *, name='launch', pty=False):
    from gateway.session_contract import Submission
    from gateway.session_local import create_local_session

    ref = create_local_session(runtime.authority, runtime.actor, {'request_id': name,
        'source': 'cli', 'model': 'fixture', 'toolsets': [], 'cwd': str(runtime.tmp_path)})
    await runtime.authority.attach(runtime.actor, ref)
    route = runtime.authority.sessions[ref.session_id].route
    runtime.plans[route] = {'command': tree.command, 'background': True, 'notify': True, 'pty': pty}
    admitted = await runtime.authority.submit(runtime.actor, Submission(
        name + '-submit', ref, {'text': 'Launch the configured background process'}, 'queue'))
    return ref, route, admitted


def _unknown_checkpoint(session):
    from tools.process_registry import _checkpoint_path

    assert session.reconciliation_required and not session.exited
    checkpoint = json.loads(_checkpoint_path().read_text())
    assert any(row['session_id'] == session.id and row['reconciliation_required'] for row in checkpoint)


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('backend', ['pipe', 'pty'])
@pytest.mark.parametrize('change', ['live', 'stop', 'epoch'])
async def test_native_os_launch_before_publication_retains_current_authority(tmp_path, monkeypatch, backend, change):
    from hermes_state_runtime import RuntimeStoreError, begin_runtime_epoch, get_session_admission

    if backend == 'pty':
        pytest.importorskip('ptyprocess')
    runtime = await _runtime(tmp_path, monkeypatch)
    tree = _tree(tmp_path, 'launch')
    entered, release = threading.Event(), threading.Event()
    track, sessions = runtime.registry._track_started, []

    def held(session, *args, **kwargs):
        sessions.append(session)
        entered.set()
        assert release.wait(15)
        return track(session, *args, **kwargs)

    monkeypatch.setattr(runtime.registry, '_track_started', held)
    ref, route, admitted = await _submit(runtime, tree, pty=backend == 'pty')
    unknown = False
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        await async_wait_until(tree.ready.exists, timeout=10, what='actual terminal before publication')
        session, = sessions
        shell, child = int(tree.shell.read_text()), int(tree.child.read_text())
        assert session.pid == shell and _alive(shell) and _alive(child)
        assert (session._pty is not None) is (backend == 'pty')
        if change == 'stop':
            try:
                await runtime.authority.interrupt(runtime.actor, ref, runtime.authority._handle(ref).execution_generation)
            except RuntimeStoreError as exc:
                assert exc.reason == 'unknown_execution'
                unknown = True
                _unknown_checkpoint(session)
            else:
                assert not _alive(shell) and not _alive(child), 'successful Stop must settle a prepublication process'
        elif change == 'epoch':
            begin_runtime_epoch(runtime.db, instance_id='replacement-during-native-publication')
        release.set()
        await async_wait_until(lambda: route in runtime.receipts, timeout=15, what='native launch consumer receipt')
        if change == 'live':
            assert not runtime.receipts[route].get('error'), runtime.receipts[route]
            tree.release.touch()
            assert await asyncio.to_thread(session._completion_event.wait, 10)
            assert session.exit_code == 0 and tree.effect.read_text() == 'finished'
        else:
            await async_wait_until(lambda: (not _alive(shell) and not _alive(child))
                                   or bool(session.reconciliation_required), timeout=10, what='stale native launch cleanup')
            if _alive(shell) or _alive(child):
                _unknown_checkpoint(session)
            if change == 'epoch':
                row = get_session_admission(runtime.db, admission_id=admitted.admission_id)
                assert (row['status'], row['outcome']) != ('terminal', 'completed'), row
            if not unknown:
                assert not tree.effect.exists()
            assert not any(item is session and not item.reconciliation_required and not item.exited
                           for item in runtime.registry._running.values()), 'stale publication cannot create a healthy executor'
    finally:
        release.set()
        await _cleanup(runtime, (tree,))


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('timing', ['published', 'no_stop', 'registration_gap'])
def test_websocket_native_stop_accounts_for_prepublication_os_launch(tmp_path, timing):
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
    from tests.gateway.fixtures.local_recovery_probe import daemon
    from tests.gateway.test_cli_plugin_command_in_gateway import _plugin_home, _Wire

    tree = _tree(tmp_path, 'wire')
    barrier, publication = tmp_path / 'launch-receipt', tmp_path / 'publish'
    marker = 'native-wire-launch-marker'
    plugin = f'''from pathlib import Path
import json

def register(ctx):
    from tools.process_registry import ProcessRegistry
    from tests.waiting import wait_until
    original = ProcessRegistry._track_started
    def held(self, session, *args, **kwargs):
        if {marker!r} in session.command:
            Path({str(barrier)!r}).write_text(json.dumps({{'pid': session.pid, 'process_id': session.id}}))
            wait_until(lambda: Path({str(publication)!r}).exists(), timeout=40, what='actual native publication gate')
        return original(self, session, *args, **kwargs)
    ProcessRegistry._track_started = held
'''
    if timing != 'registration_gap':
        publication.touch()
    with FakeLLMServer([ToolCall('terminal', {'command': tree.command + ' # ' + marker,
                                            'background': True, 'notify': True}), Text('Background launched')]) as model:
        with _plugin_home(tmp_path, plugin) as (root, home, env):
            path = home / 'config.yaml'
            config = json.loads(path.read_text())
            config['model']['base_url'] = model.base_url
            config['platform_toolsets']['cli'] = ['terminal']
            path.write_text(json.dumps(config))
            env['OPENAI_BASE_URL'] = model.base_url
            with daemon(root, home, env, barrier=False) as (proc, desc):
                wire = _Wire(home, desc)
                try:
                    sid = wire.call('session.create', request_id='wire-launch', source='cli', model='fake-model',
                        provider='custom', base_url=model.base_url, toolsets=['terminal'], cwd=str(tmp_path))['session_id']
                    admitted = wire.call('prompt.submit', session_id=sid, input_id='wire-launch-input',
                                         text='Launch the configured background process.')
                    wait_until(lambda: tree.ready.exists() and barrier.exists(), timeout=30, what='real OS launch receipt')
                    shell, child = int(tree.shell.read_text()), int(tree.child.read_text())
                    receipt = json.loads(barrier.read_text())
                    assert receipt['pid'] == shell and _alive(shell) and _alive(child)

                    def settled():
                        with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
                            return db.execute('SELECT status FROM session_admissions WHERE admission_id=?',
                                (admitted['admission_id'],)).fetchone() == ('terminal',)

                    if timing != 'registration_gap':
                        wait_until(settled, timeout=30, what='actual native parent terminal')
                    snapshot = wire.call('session.resume', session_id=sid)
                    unknown = False
                    if timing != 'no_stop':
                        try:
                            wire.call('session.interrupt', session_id=sid,
                                      execution_generation=snapshot['execution_generation'])
                        except Exception as exc:
                            assert 'unknown_execution' in str(exc), (str(exc), (home / 'restart.log').read_text())
                            unknown = True
                            checkpoint = json.loads((home / 'processes.json').read_text())
                            assert any(row['session_id'] == receipt['process_id']
                                       and row['reconciliation_required'] for row in checkpoint)
                        else:
                            assert not _alive(shell) and not _alive(child), 'Stop acknowledged an unaccounted OS launch'
                    tree.release.touch()
                    wait_until(lambda: tree.effect.exists() or not _alive(shell), timeout=10, what='actual post-Stop outcome')
                    publication.touch()
                    wait_until(settled, timeout=30, what='native launch closeout')
                    if timing == 'no_stop':
                        assert tree.effect.read_text() == 'finished'
                    elif not unknown:
                        assert not tree.effect.exists()
                finally:
                    publication.touch()
                    tree.release.touch()
                    wire.close()
                    proc.send_signal(signal.SIGINT)
                    proc.wait(timeout=20)


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
async def test_late_os_receipt_retains_cancelled_stop_cleanup_and_unrelated_native_work(tmp_path, monkeypatch):
    from gateway.session_runtime_workers import mutation_tasks, uncounted_runtime_work
    from tools import process_registry

    runtime = await _runtime(tmp_path, monkeypatch)
    old, other = _tree(tmp_path, 'late'), _tree(tmp_path, 'other')
    marker = 'native-late-os-receipt'
    old.command += ' # ' + marker
    entered, receipt, killing, cleanup = (threading.Event() for _ in range(4))
    actual_spawn, actual_kill = process_registry.subprocess.Popen, runtime.registry.kill_process
    stopping = None

    def spawn_then_wait(*args, **kwargs):
        proc = actual_spawn(*args, **kwargs)
        if marker in str(args[0] if args else kwargs.get('args')):
            entered.set()
            assert receipt.wait(20)
        return proc

    def kill_then_wait(process_id, **kwargs):
        if not killing.is_set():
            killing.set()
            assert cleanup.wait(20)
        return actual_kill(process_id, **kwargs)

    monkeypatch.setattr(process_registry.subprocess, 'Popen', spawn_then_wait)
    monkeypatch.setattr(runtime.registry, 'kill_process', kill_then_wait)
    ref, route, _ = await _submit(runtime, old)
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        await async_wait_until(old.ready.exists, timeout=10, what='OS process before Popen receipt')
        shell, child = int(old.shell.read_text()), int(old.child.read_text())
        assert _alive(shell) and _alive(child)
        stopping = asyncio.create_task(runtime.authority.interrupt(runtime.actor, ref,
            runtime.authority._handle(ref).execution_generation))
        assert await asyncio.to_thread(killing.wait, 10), 'native Stop must own the pending descriptor'
        _, separate = await _start(runtime, other, notify=True, name='other')
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)
        assert stopping.cancelled() and mutation_tasks(runtime.authority)
        assert uncounted_runtime_work(runtime.runner) > 0 and _alive(shell)
        receipt.set()
        await async_wait_until(lambda: route in runtime.receipts, timeout=15, what='stopped native late receipt')
        cleanup.set()
        await async_wait_until(lambda: not mutation_tasks(runtime.authority), timeout=15, what='retained Stop cleanup settlement')
        assert (not _alive(shell) and not _alive(child)) or any(
            session.reconciliation_required for session in runtime.registry._running.values())
        assert separate.process.poll() is None and not separate.exited
        other.release.touch()
        assert await asyncio.to_thread(separate._completion_event.wait, 10)
        assert separate.exit_code == 0 and other.effect.read_text() == 'finished' and not old.effect.exists()
    finally:
        receipt.set()
        cleanup.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        await _cleanup(runtime, (old, other))


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
async def test_native_terminal_refuses_os_spawn_when_pending_checkpoint_cannot_be_written(tmp_path, monkeypatch):
    import utils
    from tools import process_registry

    runtime = await _runtime(tmp_path, monkeypatch)
    tree = _tree(tmp_path, 'checkpoint-denied')
    marker = 'native-checkpoint-denied-command'
    tree.command += ' # ' + marker
    actual_write, actual_spawn = utils.atomic_json_write, process_registry.subprocess.Popen
    writes, spawns = [], []

    def denied_write(path, *args, **kwargs):
        if path == process_registry._checkpoint_path():
            writes.append(path)
            raise PermissionError('test native process checkpoint denied')
        return actual_write(path, *args, **kwargs)

    def observed_spawn(*args, **kwargs):
        if marker in str(args[0] if args else kwargs.get('args')):
            spawns.append(args)
        return actual_spawn(*args, **kwargs)

    monkeypatch.setattr(utils, 'atomic_json_write', denied_write)
    monkeypatch.setattr(process_registry.subprocess, 'Popen', observed_spawn)
    try:
        _, route, _ = await _submit(runtime, tree)
        await async_wait_until(lambda: route in runtime.receipts, timeout=15, what='pending checkpoint refusal')
        receipt = runtime.receipts[route]
        assert writes and not spawns and not tree.ready.exists() and not tree.effect.exists()
        assert receipt.get('error') or receipt.get('status') in {'rejected', 'unknown', 'error'}, receipt
        assert not any(session.pid or session.process or session._pty for session in runtime.registry._running.values())
    finally:
        await _cleanup(runtime, (tree,))


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
async def test_process_stop_before_native_launch_attempt_keeps_no_start_tombstone(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from gateway.session_ancillary import handlers
    from tools import process_registry

    runtime = await _runtime(tmp_path, monkeypatch)
    tree = _tree(tmp_path, 'cancel-before-start')
    marker = 'native-pre-attempt-stop-command'
    tree.command += ' # ' + marker
    entered, release = threading.Event(), threading.Event()
    actual_env, actual_spawn = runtime.registry._spawn_env, process_registry.subprocess.Popen
    spawns = []

    def held_env(*args, **kwargs):
        env = actual_env(*args, **kwargs)
        entered.set()
        assert release.wait(15)
        return env

    def observed_spawn(*args, **kwargs):
        if marker in str(args[0] if args else kwargs.get('args')):
            spawns.append(args)
        return actual_spawn(*args, **kwargs)

    monkeypatch.setattr(runtime.registry, '_spawn_env', held_env)
    monkeypatch.setattr(process_registry.subprocess, 'Popen', observed_spawn)
    ref, route, _ = await _submit(runtime, tree)
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        session, = runtime.registry._running.values()
        lease = session.approval_task_reservation.lease
        assert lease.live() and session.pid is None and not session._local_launch_attempted
        assert not spawns and not tree.ready.exists()
        connection = SimpleNamespace(authority=runtime.authority, actor=runtime.actor)
        stopped = await handlers(connection)['process.stop'](ref, {'session_id': ref.session_id})
        assert stopped['killed'] == 1 and not stopped.get('unconfirmed'), stopped
        assert lease.live(), 'resource Stop must not depend on revoking the admitted parent'
        assert session.exited and session.completion_reason == 'failed_start'
        assert session.pid is None and session.exit_code is None and not session.reconciliation_required
        assert not session.notify_on_complete and not spawns and not tree.ready.exists()
        assert runtime.registry._finished[session.id] is session
        release.set()
        await async_wait_until(lambda: route in runtime.receipts, timeout=15, what='cancelled pre-attempt producer return')
        assert runtime.receipts[route].get('error'), runtime.receipts[route]
        assert not spawns and not tree.ready.exists() and not tree.effect.exists()
        assert runtime.registry._finished[session.id] is session and not session._local_launch_attempted
        checkpoint = json.loads(process_registry._checkpoint_path().read_text())
        assert all(row['session_id'] != session.id for row in checkpoint)
        foreign = process_registry.ProcessRegistry()
        foreign._write_checkpoint()
        cold = process_registry.ProcessRegistry()
        assert cold.recover_from_checkpoint() == 0
        assert not cold._running and not foreign._running
        assert all(row['session_id'] != session.id
                   for row in json.loads(process_registry._checkpoint_path().read_text()))
    finally:
        release.set()
        await _cleanup(runtime, (tree,))
