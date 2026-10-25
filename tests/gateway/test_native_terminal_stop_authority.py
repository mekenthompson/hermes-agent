"""Native Stop reconciles terminal children after their admitted parent has settled."""

import asyncio
import json
import shlex
import signal
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import psutil
import pytest

from tests.waiting import async_wait_until, wait_until


def _alive(pid):
    try:
        return bool(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _tree(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    shell, child, ready, release, effect = [root / item for item in ('shell.pid', 'child.pid', 'ready', 'release', 'effect')]
    command = ('echo $$ > ' + shlex.quote(str(shell)) + '; sleep 120 & terminal_child=$!; echo "$terminal_child" > '
        + shlex.quote(str(child)) + '; touch ' + shlex.quote(str(ready)) + '; while [ ! -e '
        + shlex.quote(str(release)) + ' ]; do sleep 0.05; done; printf finished > ' + shlex.quote(str(effect))
        + '; kill "$terminal_child"; wait "$terminal_child" 2>/dev/null || true')
    return SimpleNamespace(shell=shell, child=child, ready=ready, release=release, effect=effect, command=command)


async def _runtime(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal
    from hermes_state import SessionDB
    from model_tools import handle_function_call
    from tools import process_registry, terminal_tool
    from tools.approval_context import current_approval_task_context

    home = tmp_path / 'home'
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('TERMINAL_CWD', str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=home / 'sessions'))
    db = SessionDB(home / 'state.db')
    runner.session_store._db = runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id=str(home.resolve()), instance_id='owner', db=db)
    actor = Principal('native-user', authority.profile_id, frozenset({
        'session:create', 'session:submit', 'session:read', 'session:control'}), 'viewer')
    registry = process_registry.ProcessRegistry()
    monkeypatch.setattr(process_registry, 'process_registry', registry)
    config = terminal_tool._get_env_config
    monkeypatch.setattr(terminal_tool, '_get_env_config', lambda: {**config(), 'env_type': 'local', 'cwd': str(tmp_path)})
    plans, receipts, origins = {}, {}, {}

    async def model(**_kwargs):
        origin = current_approval_task_context()
        assert origin is not None
        origins[origin.session] = origin
        entry = runner.session_store.lookup_by_session_key(origin.session)
        assert entry is not None
        owner = entry.session_id
        receipts[origin.session] = json.loads(await asyncio.to_thread(handle_function_call, 'terminal',
            plans[origin.session], task_id=owner))
        return {'final_response': 'Background process started', 'messages': []}

    monkeypatch.setattr(runner, '_run_agent', model)
    return SimpleNamespace(runner=runner, authority=authority, actor=actor, db=db, registry=registry,
                           plans=plans, receipts=receipts, origins=origins, tmp_path=tmp_path)


async def _start(runtime, tree, *, notify, persist=False, name='first'):
    from gateway.session_contract import Submission
    from gateway.session_local import create_local_session
    from hermes_state_runtime import get_session_admission

    ref = create_local_session(runtime.authority, runtime.actor, {
        'request_id': name, 'source': 'cli', 'model': 'fixture', 'toolsets': [], 'cwd': str(runtime.tmp_path)})
    await runtime.authority.attach(runtime.actor, ref)
    route = runtime.authority.sessions[ref.session_id].route
    runtime.plans[route] = {'command': tree.command, 'background': True, 'notify': notify,
                            'persist_on_release': persist}
    admission = await runtime.authority.submit(runtime.actor, Submission(
        name + '-submit', ref, {'text': 'Run this process until I stop it'}, 'queue'))
    await async_wait_until(lambda: route in runtime.receipts and get_session_admission(
        runtime.db, admission_id=admission.admission_id)['status'] == 'terminal',
        timeout=15, what='actual native parent settlement')
    assert get_session_admission(runtime.db, admission_id=admission.admission_id)['outcome'] == 'completed'
    receipt = runtime.receipts[route]
    assert not receipt.get('error'), receipt
    session = runtime.registry.get(receipt['session_id'])
    await async_wait_until(tree.ready.exists, timeout=10, what='actual terminal child start')
    assert session.pid and session.exit_code is None and not session.exited
    assert runtime.origins[route].task_id == admission.admission_id
    return ref, session


async def _cleanup(runtime, trees):
    for tree in trees:
        tree.release.touch()
    await asyncio.to_thread(runtime.registry.kill_all, source='test-cleanup')
    await asyncio.gather(*(live.task for live in runtime.authority.sessions.values()
                           if live.task is not None), return_exceptions=True)
    runtime.db.close()


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('notify', [False, True])
@pytest.mark.parametrize('persist', [False, True])
@pytest.mark.parametrize('stopped', [False, True])
async def test_native_stop_reaps_settled_parents_terminal_tree_only(tmp_path, monkeypatch, notify, persist, stopped):
    runtime = await _runtime(tmp_path, monkeypatch)
    own, other = _tree(tmp_path, 'own'), _tree(tmp_path, 'other')
    try:
        ref, session = await _start(runtime, own, notify=notify, persist=persist)
        _, separate = await _start(runtime, other, notify=notify, persist=persist, name='other')
        shell, child = int(own.shell.read_text()), int(own.child.read_text())
        other_shell, other_child = int(other.shell.read_text()), int(other.child.read_text())
        assert _alive(shell) and _alive(child) and _alive(other_shell) and _alive(other_child)
        assert session.persist_on_release is persist and separate.persist_on_release is persist
        if stopped:
            try:
                await runtime.authority.interrupt(runtime.actor, ref, runtime.authority._handle(ref).execution_generation)
            except Exception as exc:
                raise AssertionError({'pid': session.pid, 'shell': shell, 'child': child,
                    'captured_birth': session.host_start_time,
                    'current_birth': runtime.registry._safe_host_start_time(session.pid),
                    'returncode': session.process.poll(), 'reconciliation_required': session.reconciliation_required}) from exc
            assert not _alive(shell) and not _alive(child), vars(session)
            assert _alive(other_shell) and _alive(other_child), 'Stop must preserve another conversation executor'
            assert session.exited and session.process.poll() is not None
        own.release.touch()
        other.release.touch()
        assert await asyncio.to_thread(separate._completion_event.wait, 10)
        assert separate.exit_code == 0 and other.effect.read_text() == 'finished'
        if stopped:
            assert not own.effect.exists()
        else:
            assert await asyncio.to_thread(session._completion_event.wait, 10)
            assert session.exit_code == 0 and own.effect.read_text() == 'finished'
    finally:
        await _cleanup(runtime, (own, other))


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
async def test_native_stop_retains_actual_terminal_process_when_cleanup_fails(tmp_path, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    from tools import process_registry

    runtime = await _runtime(tmp_path, monkeypatch)
    own = _tree(tmp_path, 'denied')
    try:
        ref, session = await _start(runtime, own, notify=True)
        shell, child = int(own.shell.read_text()), int(own.child.read_text())

        def deny(*_args, **_kwargs):
            raise PermissionError('test cleanup signals denied')

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(runtime.registry, '_terminate_host_pid', deny)
            with pytest.raises(RuntimeStoreError) as failure:
                await runtime.authority.interrupt(runtime.actor, ref, runtime.authority._handle(ref).execution_generation)
            assert failure.value.reason == 'unknown_execution'
            assert _alive(shell) and _alive(child) and not own.effect.exists()
            assert not session.exited and session.exit_code is None and session.reconciliation_required
            assert runtime.registry.poll(session.id)['status'] == 'unknown'
            checkpoint = json.loads(process_registry._checkpoint_path().read_text())
            assert any(row['session_id'] == session.id and row['pid'] == shell
                       and row['reconciliation_required'] for row in checkpoint)
    finally:
        await _cleanup(runtime, (own,))


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
async def test_native_stop_keeps_remote_inspection_failure_unknown_without_host_pid_kill(tmp_path, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    from tools import process_registry, terminal_tool

    runtime = await _runtime(tmp_path, monkeypatch)
    own = _tree(tmp_path, 'remote')
    remote_dir = tmp_path / 'remote-artifacts'
    remote_dir.mkdir()
    monkeypatch.setattr(runtime.registry, '_env_temp_dir', lambda _env: str(remote_dir))
    spawn = terminal_tool.spawn_background_process

    def remote_spawn(**kwargs):
        return spawn(**{**kwargs, 'env_type': 'fixture-remote'})

    monkeypatch.setattr(terminal_tool, 'spawn_background_process', remote_spawn)
    session = None
    try:
        ref, session = await _start(runtime, own, notify=True)
        assert session.pid_scope == 'sandbox' and session.env_ref is not None
        shell, child = int(own.shell.read_text()), int(own.child.read_text())

        def failed_inspection(*_args, **_kwargs):
            raise OSError('test remote inspection unavailable')

        def no_host_kill(*_args, **_kwargs):
            pytest.fail('A sandbox PID cannot be used as a host kill identity')

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(session.env_ref, 'execute', failed_inspection)
            patch.setattr(runtime.registry, '_terminate_host_pid', no_host_kill)
            with pytest.raises(RuntimeStoreError) as failure:
                await runtime.authority.interrupt(runtime.actor, ref, runtime.authority._handle(ref).execution_generation)
            assert failure.value.reason == 'unknown_execution'
            assert _alive(shell) and _alive(child) and not own.effect.exists()
            assert not session.exited and session.exit_code is None and session.reconciliation_required
            assert runtime.registry.poll(session.id)['status'] == 'unknown'
            checkpoint = json.loads(process_registry._checkpoint_path().read_text())
            assert any(row['session_id'] == session.id and row['pid_scope'] == 'sandbox'
                       and row['reconciliation_required'] for row in checkpoint)
    finally:
        own.release.touch()
        if session is not None:
            exit_path = remote_dir / f'hermes_bg_{session.id}.exit'
            await async_wait_until(exit_path.exists, timeout=10, what='actual remote wrapper test exit')
            await asyncio.to_thread(session._reader_thread.join, 5)
        runtime.db.close()


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('stopped', [False, True])
def test_websocket_native_parent_settlement_preserves_then_stops_real_terminal_child(tmp_path, stopped):
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
    from tests.gateway.fixtures.local_recovery_probe import daemon
    from tests.gateway.test_cli_plugin_command_in_gateway import _plugin_home, _Wire

    own = _tree(tmp_path, 'wire')
    with FakeLLMServer([ToolCall('terminal', {'command': own.command, 'background': True,
                                            'persist_on_release': True}), Text('Background child started')]) as model:
        with _plugin_home(tmp_path, 'def register(ctx):\n    pass\n') as (root, home, env):
            config_path = home / 'config.yaml'
            config = json.loads(config_path.read_text())
            config['model']['base_url'] = model.base_url
            config['platform_toolsets']['cli'] = ['terminal']
            config_path.write_text(json.dumps(config))
            env['OPENAI_BASE_URL'] = model.base_url
            with daemon(root, home, env, barrier=False) as (proc, desc):
                caller, controller = _Wire(home, desc), _Wire(home, desc)
                try:
                    created = controller.call('session.create', request_id='terminal-wire', source='cli',
                        model='fake-model', provider='custom', base_url=model.base_url,
                        toolsets=['terminal'], cwd=str(tmp_path))
                    sid = created['session_id']
                    caller.call('session.resume', session_id=sid)
                    admitted = caller.call('prompt.submit', session_id=sid, input_id='terminal-wire-start',
                                           text='Run the configured background shell until I stop it.')

                    def settled():
                        with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
                            return db.execute('SELECT status,outcome FROM session_admissions WHERE admission_id=?',
                                (admitted['admission_id'],)).fetchone() == ('terminal', 'completed')

                    wait_until(settled, timeout=30, what='actual WebSocket native model parent terminal')
                    wait_until(own.ready.exists, timeout=10, what='actual model terminal process')
                    shell, child = int(own.shell.read_text()), int(own.child.read_text())
                    assert _alive(shell) and _alive(child)
                    processes = controller.call('process.list', session_id=sid)['processes']
                    assert processes and any(p['status'] == 'running' for p in processes), processes
                    snapshot = controller.call('session.resume', session_id=sid)
                    assert _alive(shell) and _alive(child), 'opening a subscriber must preserve the executor'
                    if stopped:
                        try:
                            controller.call('session.interrupt', session_id=sid,
                                            execution_generation=snapshot['execution_generation'])
                        except Exception as exc:
                            raise AssertionError((str(exc), (home / 'restart.log').read_text())) from exc
                        assert not _alive(shell) and not _alive(child)
                    own.release.touch()
                    wait_until(lambda: not _alive(shell) and not _alive(child), timeout=10,
                               what='actual model terminal process settlement')
                    if stopped:
                        assert not own.effect.exists()
                    else:
                        assert own.effect.read_text() == 'finished'
                finally:
                    own.release.touch()
                    caller.close()
                    controller.close()
                    proc.send_signal(signal.SIGINT)
                    proc.wait(timeout=20)


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
async def test_cancelled_stop_observer_retains_old_cleanup_without_killing_new_native_instruction(tmp_path, monkeypatch):
    from gateway.session_contract import Submission
    from gateway.session_runtime_workers import mutation_tasks, uncounted_runtime_work
    from hermes_state_runtime import get_session_admission

    runtime = await _runtime(tmp_path, monkeypatch)
    old, new = _tree(tmp_path, 'old'), _tree(tmp_path, 'new')
    entered, release = threading.Event(), threading.Event()
    actual_kill = runtime.registry.kill_process
    stopping = None
    try:
        ref, session = await _start(runtime, old, notify=True)
        route = runtime.authority.sessions[ref.session_id].route

        def held_kill(process_id, **kwargs):
            if process_id == session.id:
                entered.set()
                assert release.wait(15)
            return actual_kill(process_id, **kwargs)

        monkeypatch.setattr(runtime.registry, 'kill_process', held_kill)
        stopping = asyncio.create_task(runtime.authority.interrupt(runtime.actor, ref,
            runtime.authority._handle(ref).execution_generation))
        assert await asyncio.to_thread(entered.wait, 10)
        runtime.plans[route] = {'command': new.command, 'background': True, 'notify': True}
        previous = runtime.receipts.pop(route)
        admission = await runtime.authority.submit(runtime.actor, Submission('new-instruction', ref,
            {'text': 'Start this distinct new background process'}, 'queue'))
        await async_wait_until(lambda: route in runtime.receipts and get_session_admission(runtime.db,
            admission_id=admission.admission_id)['status'] == 'terminal', timeout=15, what='new authorized native parent')
        newer = runtime.registry.get(runtime.receipts[route]['session_id'])
        await async_wait_until(new.ready.exists, timeout=10, what='new instruction physical process')
        assert newer.id != previous['session_id'] and newer.process.poll() is None
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)
        assert stopping.cancelled() and mutation_tasks(runtime.authority)
        assert uncounted_runtime_work(runtime.runner) > 0 and session.process.poll() is None
        release.set()
        await async_wait_until(lambda: not mutation_tasks(runtime.authority), timeout=15, what='actual detached Stop cleanup')
        assert session.process.poll() is not None and session.exited
        assert newer.process.poll() is None and not newer.exited, 'old Stop must not select work admitted afterward'
        new.release.touch()
        assert await asyncio.to_thread(newer._completion_event.wait, 10)
        assert newer.exit_code == 0 and new.effect.read_text() == 'finished' and not old.effect.exists()
    finally:
        release.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        await _cleanup(runtime, (old, new))
