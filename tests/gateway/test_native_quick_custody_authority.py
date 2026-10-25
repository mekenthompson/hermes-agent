"""Quick-command custody survives session escape and double-fork reparenting."""

import asyncio
import json
import os
import shlex
import sys
import threading
from types import SimpleNamespace

import psutil
import pytest

from tests.gateway.fixtures.local_recovery_probe import daemon
from tests.gateway.test_cli_plugin_command_in_gateway import _plugin_home, _Wire
from tests.waiting import async_wait_until, wait_until


def _alive(pid):
    try:
        return bool(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _escaped_child(tmp_path, mode):
    script, parent, pid, release, effect = [tmp_path / name for name in (
        'escaped.py', 'parent.pid', 'child.pid', 'release', 'effect')]
    script.write_text('''import os, sys, time
from pathlib import Path
mode, pidfile, release, effect = sys.argv[1:]
if mode == 'doublefork':
    if os.fork():
        os._exit(0)
    os.setsid()
    if os.fork():
        os._exit(0)
else:
    os.setsid()
Path(pidfile).write_text(str(os.getpid()))
while not Path(release).exists():
    time.sleep(.01)
Path(effect).write_text('escaped child completed')
''')
    launch = shlex.join([sys.executable, str(script), mode, str(pid), str(release), str(effect)])
    command = ('echo $$ > ' + shlex.quote(str(parent)) + '; ' + launch
        + ' </dev/null >/dev/null 2>&1 & while [ ! -s ' + shlex.quote(str(pid))
        + ' ]; do sleep .01; done; printf launched')
    return SimpleNamespace(parent=parent, pid=pid, release=release, effect=effect, command=command)


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('mode', ['setsid', 'doublefork'])
@pytest.mark.parametrize('stopped', [False, True])
def test_actual_wire_quick_child_escaping_shell_session_stays_in_native_custody(tmp_path, mode, stopped):
    tree = _escaped_child(tmp_path, mode)
    with _plugin_home(tmp_path, 'def register(ctx):\n    pass\n') as (root, home, env):
        path = home / 'config.yaml'
        config = json.loads(path.read_text())
        config['quick_commands'] = {'escape-child': {'type': 'exec', 'command': tree.command}}
        path.write_text(json.dumps(config))
        with daemon(root, home, env, barrier=False) as (_proc, desc):
            wire = _Wire(home, desc)
            child = None
            try:
                sid = wire.call('session.create', request_id='native-escape-' + mode, source='cli',
                                toolsets=[], cwd=str(tmp_path))['session_id']
                receipt = wire.call('slash.exec', session_id=sid, command='/escape-child')
                assert receipt['type'] == 'exec' and receipt['output'].startswith('launched'), receipt
                child = int(tree.pid.read_text())
                parent = int(tree.parent.read_text())
                assert _alive(child) and not _alive(parent)
                assert os.getsid(child) != parent and os.getpgid(child) != parent, 'the real child must escape the shell group'
                snapshot = wire.call('session.resume', session_id=sid)
                unknown = False
                if stopped:
                    try:
                        wire.call('session.interrupt', session_id=sid,
                                  execution_generation=snapshot['execution_generation'])
                    except Exception as exc:
                        assert 'unknown_execution' in str(exc), (str(exc), (home / 'restart.log').read_text())
                        unknown = True
                        records = json.loads((home / 'processes.json').read_text())
                        assert any(row.get('reconciliation_required') for row in records), records
                    else:
                        assert not _alive(child), 'successful native Stop lost an escaped physical child'
                tree.release.touch()
                if _alive(child):
                    wait_until(tree.effect.exists, timeout=10, what='escaped actual child external effect')
                if not stopped:
                    assert tree.effect.read_text() == 'escaped child completed'
                elif not unknown:
                    assert not tree.effect.exists()
            finally:
                tree.release.touch()
                if child is not None:
                    wait_until(lambda: not _alive(child), timeout=10, what='naturally released escaped test child')
                wire.close()


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
@pytest.mark.parametrize('mode', ['setsid', 'doublefork'])
@pytest.mark.parametrize('backend', ['pipe', 'pty'])
@pytest.mark.parametrize('stopped', [False, True])
async def test_native_background_terminal_keeps_escaped_children_in_its_execution_scope(
        tmp_path, monkeypatch, mode, backend, stopped):
    from hermes_state_runtime import RuntimeStoreError, get_session_admission
    from tests.gateway.test_native_terminal_launch_stop_authority import _submit
    from tests.gateway.test_native_terminal_stop_authority import _cleanup, _runtime
    from tools.process_registry import _checkpoint_path

    if backend == 'pty':
        pytest.importorskip('ptyprocess')
    tree = _escaped_child(tmp_path, mode)
    dimensions = tmp_path / 'initial-pty-size'
    if backend == 'pty':
        tree.command = 'stty size > ' + shlex.quote(str(dimensions)) + '; ' + tree.command
    tree.ready = tree.pid
    runtime = await _runtime(tmp_path, monkeypatch)
    child = None
    try:
        ref, route, admitted = await _submit(runtime, tree, pty=backend == 'pty')
        await async_wait_until(lambda: route in runtime.receipts and get_session_admission(runtime.db,
            admission_id=admitted.admission_id)['status'] == 'terminal', timeout=15, what='native custody parent settlement')
        assert get_session_admission(runtime.db, admission_id=admitted.admission_id)['outcome'] == 'completed'
        receipt = runtime.receipts[route]
        assert not receipt.get('error'), receipt
        process = runtime.registry.get(receipt['session_id'])
        await async_wait_until(lambda: tree.pid.exists() and tree.pid.read_text().strip(), timeout=10,
                               what='actual native escaped child')
        child = int(tree.pid.read_text())
        if backend == 'pty':
            assert dimensions.read_text().split() == ['30', '120'], 'actual PTY startup dimensions must precede exec'
        parent = int(tree.parent.read_text())
        await async_wait_until(lambda: not _alive(parent), timeout=10, what='initial native terminal shell exit')
        assert _alive(child) and os.getsid(child) != parent and os.getpgid(child) != parent
        assert not process.exited and process.exit_code is None and not process._completion_event.is_set(), (
            'initial shell exit must not complete native work with a live escaped child', process.id)
        records = json.loads(_checkpoint_path().read_text())
        assert any(row['session_id'] == process.id for row in records)
        unknown = False
        if stopped:
            try:
                await runtime.authority.interrupt(runtime.actor, ref,
                                                   runtime.authority._handle(ref).execution_generation)
            except RuntimeStoreError as exc:
                assert exc.reason == 'unknown_execution'
                unknown = True
                assert process.reconciliation_required and not process.exited
                records = json.loads(_checkpoint_path().read_text())
                assert any(row['session_id'] == process.id and row['reconciliation_required'] for row in records)
            else:
                assert not _alive(child), 'successful Stop must settle descendants that escaped the shell group'
        tree.release.touch()
        if not stopped:
            assert await asyncio.to_thread(process._completion_event.wait, 10)
            assert process.exited and process.exit_code == 0
            assert tree.effect.read_text() == 'escaped child completed'
        elif not unknown:
            assert not tree.effect.exists()
    finally:
        tree.release.touch()
        await _cleanup(runtime, (tree,))
        if child is not None:
            await async_wait_until(lambda: not _alive(child), timeout=10, what='released native escaped child exit')


def _configure(home, tree):
    path = home / 'config.yaml'
    config = json.loads(path.read_text())
    config['quick_commands'] = {'escape-child': {'type': 'exec', 'command': tree.command}}
    path.write_text(json.dumps(config))


def _launch(wire, tree, request_id):
    sid = wire.call('session.create', request_id=request_id, source='cli',
                    toolsets=[], cwd=str(tree.pid.parent))['session_id']
    receipt = wire.call('slash.exec', session_id=sid, command='/escape-child')
    assert receipt['output'].startswith('launched'), receipt
    child = int(tree.pid.read_text())
    assert _alive(child)
    return sid, child


def _unresolved(home):
    path = home / 'processes.json'
    records = json.loads(path.read_text()) if path.exists() else []
    return records if any(row.get('reconciliation_required') for row in records) else None


@pytest.mark.platforms('linux')
def test_actual_custody_holder_death_retains_unproven_escaped_child(tmp_path):
    tree = _escaped_child(tmp_path, 'doublefork')
    child = None
    with _plugin_home(tmp_path, 'def register(ctx):\n    pass\n') as (root, home, env):
        _configure(home, tree)
        with daemon(root, home, env, barrier=False) as (owner, desc):
            wire = _Wire(home, desc)
            try:
                sid, child = _launch(wire, tree, 'custody-holder-loss')
                holder = psutil.Process(child).parent()
                assert holder is not None and holder.pid not in {1, owner.pid}, 'a private holder must keep kernel parentage'
                actual_descendants = {proc.pid: proc.create_time() for proc in psutil.Process(owner.pid).children(recursive=True)}
                assert actual_descendants.get(holder.pid) == holder.create_time(), 'fault target must be this native execution holder'
                assert json.loads((home / 'processes.json').read_text())
                holder.kill()  # psutil revalidates the captured OS process identity.
                wait_until(lambda: _unresolved(home), timeout=10, what='lost custody control retained as unknown')
                snapshot = wire.call('session.resume', session_id=sid)
                with pytest.raises(Exception, match='unknown_execution'):
                    wire.call('session.interrupt', session_id=sid,
                              execution_generation=snapshot['execution_generation'])
                assert _unresolved(home) and not tree.effect.exists()
            finally:
                tree.release.touch()
                if child is not None:
                    wait_until(lambda: not _alive(child), timeout=10, what='released child after actual keeper death')
                wire.close()


@pytest.mark.platforms('linux')
def test_native_owner_restart_does_not_replay_or_infer_custody_completion(tmp_path):
    tree = _escaped_child(tmp_path, 'setsid')
    child = None
    with _plugin_home(tmp_path, 'def register(ctx):\n    pass\n') as (root, home, env):
        _configure(home, tree)
        try:
            with daemon(root, home, env, barrier=False) as (owner, desc):
                wire = _Wire(home, desc)
                try:
                    sid, child = _launch(wire, tree, 'custody-owner-restart')
                    original_pid_text = tree.pid.read_text()
                    assert json.loads((home / 'processes.json').read_text()), 'native custody must be durable before reply'
                    owner.kill()
                    owner.wait(timeout=10)
                finally:
                    wire.close()
            with daemon(root, home, env, barrier=False) as (_replacement, desc):
                wire = _Wire(home, desc)
                try:
                    snapshot = wire.call('session.resume', session_id=sid)
                    wait_until(lambda: _unresolved(home), timeout=10, what='cold native custody uncertainty')
                    assert tree.pid.read_text() == original_pid_text, 'restoring a conversation must not rerun its command'
                    with pytest.raises(Exception, match='unknown_execution'):
                        wire.call('session.interrupt', session_id=sid,
                                  execution_generation=snapshot['execution_generation'])
                    assert _unresolved(home)
                finally:
                    wire.close()
        finally:
            tree.release.touch()
            if child is not None:
                wait_until(lambda: not _alive(child), timeout=10, what='released child after native owner restart')


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
async def test_ready_custody_can_be_stopped_without_target_fork_or_parent_revocation(tmp_path, monkeypatch):
    from gateway.session_ancillary import handlers
    from tests.gateway.test_native_terminal_launch_stop_authority import _submit
    from tests.gateway.test_native_terminal_stop_authority import _cleanup, _runtime, _tree
    from tools.process_registry_custody import LocalCustody

    runtime = await _runtime(tmp_path, monkeypatch)
    tree = _tree(tmp_path, 'ready-abort')
    marker = 'native-ready-abort-target'
    tree.command += ' # ' + marker
    entered, release = threading.Event(), threading.Event()
    controllers = []
    actual_wait = LocalCustody.wait_event

    def hold_ready(controller, event, *args, **kwargs):
        result = actual_wait(controller, event, *args, **kwargs)
        if event is controller.ready and marker in controller.session.command:
            controllers.append(controller)
            entered.set()
            assert release.wait(15)
        return result

    monkeypatch.setattr(LocalCustody, 'wait_event', hold_ready)
    ref, route, _ = await _submit(runtime, tree)
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        custody, = controllers
        session = custody.session
        assert custody.proc.poll() is None and custody.ready.is_set() and not custody.started.is_set()
        lease = session.approval_task_reservation.lease
        assert lease.live() and not tree.ready.exists()
        connection = SimpleNamespace(authority=runtime.authority, actor=runtime.actor)
        stopped = await handlers(connection)['process.stop'](ref, {'session_id': ref.session_id})
        assert stopped['killed'] == 1 and not stopped.get('unconfirmed'), stopped
        assert custody.empty.is_set() and custody.proc.poll() is not None
        assert lease.live() and session.pid is None and session.exit_code is None
        release.set()
        await async_wait_until(lambda: route in runtime.receipts, timeout=15, what='aborted READY producer')
        assert not tree.ready.exists() and not tree.effect.exists()
        assert session.exited and session.completion_reason == 'failed_start'
    finally:
        release.set()
        await _cleanup(runtime, (tree,))


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
async def test_cancelled_stop_observer_cannot_release_a_held_go_custody_scope(tmp_path, monkeypatch):
    from gateway.session_runtime_workers import mutation_tasks, uncounted_runtime_work
    from tests.gateway.test_native_terminal_launch_stop_authority import _submit
    from tests.gateway.test_native_terminal_stop_authority import _cleanup, _runtime, _tree
    from tools.process_registry_custody import LocalCustody

    runtime = await _runtime(tmp_path, monkeypatch)
    tree = _tree(tmp_path, 'late-go')
    marker = 'native-late-go-target'
    tree.command += ' # ' + marker
    entered, release = threading.Event(), threading.Event()
    controllers = []
    actual_send = LocalCustody.send

    def hold_go(controller, operation, **values):
        if operation == 'go' and marker in controller.session.command:
            controllers.append(controller)
            entered.set()
            assert release.wait(15)
        return actual_send(controller, operation, **values)

    monkeypatch.setattr(LocalCustody, 'send', hold_go)
    ref, route, _ = await _submit(runtime, tree)
    stopping = None
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        custody, = controllers
        arrival = runtime.authority.route_epoch(route)
        stopping = asyncio.create_task(runtime.authority.interrupt(runtime.actor, ref,
            runtime.authority._handle(ref).execution_generation))
        await async_wait_until(lambda: runtime.authority.route_epoch(route) > arrival,
                               timeout=10, what='native Stop while GO commit remains locked')
        await async_wait_until(lambda: custody.empty.is_set() and custody.proc.poll() is not None,
                               timeout=10, what='physical custody abort while GO receipt remains held')
        assert not stopping.done(), 'Stop cannot close bookkeeping while its descriptor commit is held'
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)
        assert stopping.cancelled() and mutation_tasks(runtime.authority) and uncounted_runtime_work(runtime.runner) > 0
        assert not tree.ready.exists()
        release.set()
        await async_wait_until(lambda: route in runtime.receipts and not mutation_tasks(runtime.authority),
                               timeout=15, what='retained late GO cleanup')
        assert custody.empty.is_set() and custody.proc.poll() is not None
        assert custody.session.exited and not tree.effect.exists()
    finally:
        release.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        await _cleanup(runtime, (tree,))


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
@pytest.mark.parametrize('change', ['live', 'stop', 'epoch', 'descendants', 'holder_death'])
async def test_native_api_foreground_custody_uses_its_claim_without_background_consent(tmp_path, monkeypatch, change):
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.session import build_session_context
    from gateway.session_api_turn import admit_api_turn, api_execution, observe_api_turn
    from gateway.session_results import execution_result
    from hermes_state_runtime import begin_runtime_epoch, get_session_admission
    from model_tools import handle_function_call
    from tests.gateway.test_native_terminal_stop_authority import _runtime
    from tools.approval_context import current_approval_task_context
    from tools.process_registry_custody import LocalCustody

    runtime = await _runtime(tmp_path, monkeypatch)
    runner, authority, actor, registry = runtime.runner, runtime.authority, runtime.actor, runtime.registry
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.gateway_runner, adapter._session_db = runner, authority.db
    runner.adapters[Platform.API_SERVER] = adapter
    entered, release = threading.Event(), threading.Event()
    controllers, receipts = [], []
    effect = tmp_path / 'api-foreground-effect'
    marker = 'native-api-foreground-target'
    tree = _escaped_child(tmp_path, 'doublefork') if change in {'descendants', 'holder_death'} else None
    command = (tree.command.replace('2>&1 & while', '2>&1; while') if tree
               else 'printf foreground > ' + shlex.quote(str(effect)))
    actual_wait = LocalCustody.wait_event

    def hold_ready(controller, event, *args, **kwargs):
        result = actual_wait(controller, event, *args, **kwargs)
        if event is controller.ready and marker in controller.session.command:
            controllers.append(controller)
            entered.set()
            assert release.wait(15)
        return result

    async def model(_event):
        assert current_approval_task_context() is None
        assert callable(api_execution.get()['native_effect_current'])
        live = authority.sessions[admitted[1].session_id]
        entry = runner.session_store.lookup_by_session_key(live.route)
        with runner._session_env_scope(build_session_context(live.source, runner.config, entry)):
            receipts.append(json.loads(await asyncio.to_thread(handle_function_call, 'terminal',
                {'command': command + ' # ' + marker}, task_id='api-fg-custody')))
        execution_result.get()['result'] = {'final_response': 'checked', 'messages': []}
        return 'checked'

    monkeypatch.setattr(LocalCustody, 'wait_event', hold_ready)
    monkeypatch.setattr(runner, '_handle_message', model)
    admitted = admit_api_turn(adapter, user_message='Run the configured foreground command',
                              conversation_history=[], request_id='api-custody-' + change, session_id='api-custody-' + change)
    observing = asyncio.create_task(observe_api_turn(admitted))
    try:
        assert await asyncio.to_thread(entered.wait, 10), (receipts, observing.result() if observing.done() else 'observer pending', registry.list_sessions())
        custody, = controllers
        assert getattr(custody.session, 'approval_task_reservation', None) is None, 'foreground API execution cannot mint background consent'
        assert custody.session.pid is None and not effect.exists()
        if change == 'stop':
            await authority.interrupt(actor, admitted[1], authority._handle(admitted[1]).execution_generation)
        elif change == 'epoch':
            begin_runtime_epoch(authority.db, instance_id='api-owner-replacement')
        release.set()
        if tree is not None:
            await async_wait_until(lambda: custody.main_exited.is_set() and tree.pid.exists(), timeout=10,
                                   what='foreground shell exit with escaped child')
            assert _alive(int(tree.pid.read_text())) and not receipts
            assert not custody.settled() and not custody.session.exited
            if change == 'holder_death':
                holder = psutil.Process(custody.session.custody_pid)
                assert holder.pid == custody.proc.pid
                assert registry._safe_host_start_time(holder.pid) == custody.session.custody_start_time
                assert psutil.Process(int(tree.pid.read_text())).parent().pid == holder.pid
                holder.kill()  # Captured test-only keeper identity; never signal its reparented child.
            else:
                await authority.interrupt(actor, admitted[1], authority._handle(admitted[1]).execution_generation)
                assert not _alive(int(tree.pid.read_text())) and not tree.effect.exists()
        await async_wait_until(lambda: bool(receipts), timeout=15, what='actual API foreground consumer result')
        receipt, = receipts
        assert getattr(custody.session, 'approval_task_reservation', None) is None
        if change == 'live':
            assert not receipt.get('error') and receipt['exit_code'] == 0, receipt
            assert effect.read_text() == 'foreground'
            await asyncio.wait_for(observing, 15)
        elif change == 'holder_death':
            assert receipt.get('code') == 'process_execution_uncertain' and receipt['status'] == 'unknown', receipt
            assert len(controllers) == 1 and _alive(int(tree.pid.read_text())) and not tree.effect.exists()
            assert not custody.session.exited and custody.session.reconciliation_required
            records = json.loads((tmp_path / 'home' / 'processes.json').read_text())
            assert any(row['session_id'] == custody.session.id and row['reconciliation_required'] for row in records)
            await asyncio.wait_for(observing, 15)
        else:
            assert receipt.get('error') and not effect.exists(), receipt
            if change == 'epoch':
                row = get_session_admission(authority.db, admission_id=admitted[2]['admission_id'])
                assert (row['status'], row['outcome']) != ('terminal', 'completed')
    finally:
        release.set()
        if tree is not None:
            tree.release.touch()
        if not observing.done():
            observing.cancel()
        await asyncio.gather(observing, return_exceptions=True)
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')
        if tree is not None and tree.pid.exists():
            await async_wait_until(lambda: not _alive(int(tree.pid.read_text())), timeout=10,
                                   what='naturally released foreground test child')
        await asyncio.gather(*(live.task for live in authority.sessions.values() if live.task is not None),
                             return_exceptions=True)
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
async def test_profile_retirement_keeps_post_output_escaped_quick_scope_owned(tmp_path, monkeypatch):
    from gateway.session_commands import execute_command
    from gateway.session_runtime_workers import (command_effects, join_authority_work,
                                                stop_authority_work, uncounted_runtime_work)
    from tests.gateway.test_native_quick_command_authority import _native_session
    from tools import process_registry

    tree = _escaped_child(tmp_path, 'doublefork')
    runtime = await _native_session(tmp_path, monkeypatch, {'escape-child': tree.command})
    registry = process_registry.ProcessRegistry()
    monkeypatch.setattr(process_registry, 'process_registry', registry)
    child = None
    try:
        receipt = await execute_command(runtime.connection, runtime.ref, {'command': '/escape-child'})
        assert receipt['output'].startswith('launched'), receipt
        child = int(tree.pid.read_text())
        assert _alive(child) and not _alive(int(tree.parent.read_text()))
        session, = registry._running.values()
        keeper = session.custody_pid
        assert session._custody.main_exited.is_set() and not session.exited
        assert command_effects(runtime.authority) and uncounted_runtime_work(runtime.authority.runner) > 0
        stop_authority_work(runtime.authority)
        await join_authority_work(runtime.authority, timeout=15)
        assert not _alive(child) and not _alive(keeper), 'profile drain lost a post-output physical scope'
        assert session.exited and session.custody_scope_empty and not session.reconciliation_required
        assert not command_effects(runtime.authority) and not tree.effect.exists()
    finally:
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')
        tree.release.touch()
        if child is not None:
            await async_wait_until(lambda: not _alive(child), timeout=10, what='released profile test descendant')
        runtime.authority.db.close()


@pytest.mark.platforms('linux')
def test_target_stdout_cannot_spoof_custody_or_stop_another_conversation(tmp_path):
    own_dir, other_dir = tmp_path / 'own', tmp_path / 'other'
    own_dir.mkdir()
    other_dir.mkdir()
    own, other = _escaped_child(own_dir, 'doublefork'), _escaped_child(other_dir, 'setsid')
    fake_empty = json.dumps({'kind': 'scope_empty', 'code': 0})
    own.command = 'printf %s ' + shlex.quote(fake_empty) + '; ' + own.command
    fresh = tmp_path / 'fresh'
    with _plugin_home(tmp_path, 'def register(ctx):\n    pass\n') as (root, home, env):
        path = home / 'config.yaml'
        config = json.loads(path.read_text())
        config['quick_commands'] = {name: {'type': 'exec', 'command': command} for name, command in {
            'escape-child': other.command, 'escape-own': own.command,
            'fresh-work': 'printf fresh > ' + shlex.quote(str(fresh))}.items()}
        path.write_text(json.dumps(config))
        children = []
        with daemon(root, home, env, barrier=False) as (_proc, desc):
            wire = _Wire(home, desc)
            try:
                other_sid, other_child = _launch(wire, other, 'other-conversation')
                children.append(other_child)
                own_sid = wire.call('session.create', request_id='own-conversation', source='cli',
                                    toolsets=[], cwd=str(own_dir))['session_id']
                receipt = wire.call('slash.exec', session_id=own_sid, command='/escape-own')
                assert fake_empty in receipt['output'] and 'launched' in receipt['output'], receipt
                own_child = int(own.pid.read_text())
                children.append(own_child)
                records = json.loads((home / 'processes.json').read_text())
                own_record, = [row for row in records if 'escape-own' not in row['command']
                               and str(own_dir / 'escaped.py') in row['command']]
                assert not own_record['custody_scope_empty'] and _alive(own_child) and _alive(other_child)
                generation = wire.call('session.resume', session_id=own_sid)['execution_generation']
                wire.call('session.interrupt', session_id=own_sid, execution_generation=generation)
                assert not _alive(own_child) and _alive(other_child) and not own.effect.exists()
                receipt = wire.call('slash.exec', session_id=other_sid, command='/fresh-work')
                assert receipt['type'] == 'exec' and fresh.read_text() == 'fresh'
                assert _alive(other_child), 'an unrelated new command must retain the earlier scope'
                other.release.touch()
                wait_until(other.effect.exists, timeout=10, what='other conversation authorized effect')
            finally:
                own.release.touch()
                other.release.touch()
                for child in children:
                    wait_until(lambda: not _alive(child), timeout=10, what='naturally released isolated child')
                wire.close()


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
async def test_persisted_background_scope_survives_owner_control_eof_without_false_receipt(tmp_path, monkeypatch):
    from tests.gateway.test_native_terminal_stop_authority import _cleanup, _runtime, _start
    from tools.process_registry import _checkpoint_path

    runtime = await _runtime(tmp_path, monkeypatch)
    tree = _escaped_child(tmp_path, 'doublefork')
    tree.ready = tree.pid
    child = None
    try:
        _ref, session = await _start(runtime, tree, notify=False, persist=True, name='persisted-escape')
        custody = session._custody
        await async_wait_until(custody.main_exited.is_set, timeout=10, what='persisted main shell exit')
        child, original_pid = int(tree.pid.read_text()), tree.pid.read_text()
        keeper = session.custody_pid
        assert session.persist_on_release and _alive(child) and _alive(keeper)
        assert not _alive(int(tree.parent.read_text())) and not session.exited
        custody.close()  # Lose only the actual private owner control channel after admitted GO.
        await async_wait_until(lambda: bool(session.reconciliation_required), timeout=10,
                               what='persisted scope uncertainty after actual owner EOF')
        assert _alive(child) and _alive(keeper), 'persist_on_release must preserve its actual kernel scope'
        assert not custody.empty.is_set() and not session.custody_scope_empty
        assert not session.exited and not session._completion_event.is_set() and not tree.effect.exists()
        def durable_unknown():
            checkpoint = json.loads(_checkpoint_path().read_text())
            return any(row['session_id'] == session.id and row['custody_pid'] == keeper
                       and row['reconciliation_required'] and not row['custody_scope_empty'] for row in checkpoint)

        await async_wait_until(durable_unknown, timeout=10, what='actual persisted EOF uncertainty checkpoint')
        assert tree.pid.read_text() == original_pid, 'losing control cannot replay the target'
        tree.release.touch()
        await async_wait_until(lambda: not _alive(child) and not _alive(keeper), timeout=10,
                               what='naturally released persisted kernel scope')
        assert tree.effect.read_text() == 'escaped child completed'
        assert not custody.empty.is_set() and not session.custody_scope_empty
        assert session.reconciliation_required and not session.exited, 'OS exit cannot replace the lost trusted receipt'
    finally:
        tree.release.touch()
        await _cleanup(runtime, (tree,))
        if child is not None:
            await async_wait_until(lambda: not _alive(child), timeout=10, what='released persisted test descendant')
