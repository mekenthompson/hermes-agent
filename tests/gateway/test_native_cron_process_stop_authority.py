"""A persisted cron fire cancels its real process scope, independently of delivery routing."""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.gateway.test_managed_process_lifecycle import _alive, _body
from tests.gateway.test_native_cron_task_authority import _fire, _runtime, _settled
from tests.waiting import async_wait_until


def _model(monkeypatch, callback):
    """Keep the real scheduler, native admission, registered tools, and process consumers."""
    from cron import scheduler
    monkeypatch.setattr(scheduler, '_resolve_cron_agent_setup',
                        lambda *_args: scheduler._CronAgentSetup(model='fixture'))

    def construct(_cls, job, _config, _setup, *, workdir, session_id, session_db):
        agent = MagicMock()
        agent.session_id = session_id
        agent._session_db = session_db
        agent._interrupt_requested = False
        agent._active_children, agent._active_children_lock = [], threading.Lock()
        def converse(prompt, **kw):
            result = callback(job, kw['task_id'])
            session_db.append_message(session_id, 'assistant', result['final_response'])
            return result
        agent.run_conversation = converse
        return agent
    monkeypatch.setattr(scheduler, '_construct_cron_agent', construct)


def _start(command, task_id):
    from model_tools import handle_function_call
    from tools.approval_context import current_approval_task_context
    evidence = current_approval_task_context()
    result = json.loads(handle_function_call('terminal', {'command': command, 'background': True,
        'persist_on_release': True}, task_id=task_id))
    assert not result.get('error'), result
    return evidence, result


@pytest.mark.platforms('linux')
@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['healthy', 'settled', 'live', 'unknown'])
async def test_cron_cancel_settles_actual_scope_or_reports_unknown(tmp_path, monkeypatch, phase):
    from gateway.session_cron import operation
    from hermes_state_keys import CRON_PRINCIPAL_ID
    from hermes_state_runtime import RuntimeStoreError
    from model_tools import handle_function_call
    from tools.process_registry_custody import LocalCustody

    _runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    tree, command = _body(tmp_path, 'body')
    release, ready, observed, late = threading.Event(), threading.Event(), [], []
    late_effect = tmp_path / 'late-tool'

    def model(_job, task_id):
        observed.append(_start(command, task_id))
        ready.set()
        if phase == 'live':
            assert release.wait(30)
            late.append(json.loads(handle_function_call('write_file',
                {'path': str(late_effect), 'content': 'after cancel'}, task_id=task_id)))
        return {'final_response': 'Background service is running', 'messages': []}

    _model(monkeypatch, model)
    try:
        _job, receipt = await _fire(authority)
        assert await asyncio.to_thread(ready.wait, 15)
        await async_wait_until((tree / 'ready').exists, timeout=10, what='actual scheduled kernel process')
        evidence, result = observed[0]
        process = registry.get(result['session_id'])
        pid = int((tree / 'pid').read_text())
        assert evidence.identity == CRON_PRINCIPAL_ID and evidence.profile == authority.profile_id
        assert evidence.task_id == receipt['admission_id'] and _alive(pid)
        if phase != 'live':
            await _settled(authority, receipt)
            status = await operation(authority, 'status', receipt)
            assert status['result'][0] is True, status
        if phase == 'healthy':
            (tree / 'release').touch()
            assert await asyncio.to_thread(process._completion_event.wait, 10)
            assert (tree / 'effect').read_text() == 'finished' and process.exit_code == 7
            return
        if phase == 'unknown':
            def denied(_self):
                raise PermissionError('Test OS custody control denied')
            with monkeypatch.context() as patch:
                patch.setattr(LocalCustody, 'stop', denied)
                with pytest.raises(RuntimeStoreError) as caught:
                    await operation(authority, 'cancel', receipt)
                assert caught.value.reason == 'unknown_execution'
            assert _alive(pid) and not process.exited and process.reconciliation_required
            from gateway.session_runtime_workers import command_unknowns
            assert process in command_unknowns(authority, authority.sessions[receipt['session_id']].route)
            records = json.loads((tmp_path / 'home' / 'processes.json').read_text())
            assert any(row['session_id'] == process.id and row['reconciliation_required'] for row in records)
        assert await operation(authority, 'cancel', receipt) == {'ok': True}
        assert process.exited and not _alive(pid), registry.poll(process.id)
        (tree / 'release').touch()  # A successful cancel cannot leave a process to perform this effect.
        assert not (tree / 'effect').exists()
        release.set()
        if phase == 'live':
            await _settled(authority, receipt)
            assert late[0].get('code') == 'task_authority_revoked' and not late_effect.exists(), late
    finally:
        release.set()
        (tree / 'release').touch()
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')


@pytest.mark.platforms('linux')
@pytest.mark.asyncio
async def test_cron_cancel_uses_original_fire_and_profile_scope(tmp_path, monkeypatch):
    from gateway.session_contract import Principal
    from gateway.session_cron import operation
    from hermes_state_runtime import RuntimeStoreError

    _runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    bodies = [_body(tmp_path, name) for name in ('first', 'other')]
    observed = []
    def model(_job, task_id):
        observed.append(_start(bodies[len(observed)][1], task_id))
        return {'final_response': 'service running', 'messages': []}
    _model(monkeypatch, model)
    try:
        _job, first = await _fire(authority)
        await _settled(authority, first)
        _job, other = await _fire(authority)
        await _settled(authority, other)
        for tree, _command in bodies:
            await async_wait_until((tree / 'ready').exists, timeout=10, what='separate actual cron body')
        processes = [registry.get(result['session_id']) for _evidence, result in observed]
        wrong = {'session_id': first['session_id'], 'admission_id': other['admission_id']}
        with pytest.raises(RuntimeStoreError, match='permission_denied'):
            await operation(authority, 'cancel', wrong)
        actor = Principal('foreign', 'another-profile', frozenset({'session:create', 'session:submit', 'session:control'}), 'viewer')
        with pytest.raises(RuntimeStoreError, match='profile_mismatch'):
            await operation(authority, 'cancel', first, actor)
        assert all(_alive(int((tree / 'pid').read_text())) for tree, _command in bodies)
        assert await operation(authority, 'cancel', first) == {'ok': True}
        assert processes[0].exited and not _alive(int((bodies[0][0] / 'pid').read_text()))
        assert not processes[1].exited and _alive(int((bodies[1][0] / 'pid').read_text()))
        bodies[0][0].joinpath('release').touch()
        bodies[1][0].joinpath('release').touch()
        assert await asyncio.to_thread(processes[1]._completion_event.wait, 10)
        assert not (bodies[0][0] / 'effect').exists()
        assert (bodies[1][0] / 'effect').read_text() == 'finished'
    finally:
        for tree, _command in bodies:
            (tree / 'release').touch()
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')


@pytest.mark.platforms('linux')
@pytest.mark.asyncio
async def test_cron_cancel_during_real_scheduler_terminal_setup_is_no_start(tmp_path, monkeypatch):
    from gateway.session_cron import operation
    from tools import terminal_tool

    _runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    tree, command = _body(tmp_path, 'body')
    waiting, release, observed = threading.Event(), threading.Event(), []
    acquire = terminal_tool._acquire_env
    def held(*args, **kwargs):
        result = acquire(*args, **kwargs)
        waiting.set()
        assert release.wait(30)
        return result
    def model(_job, task_id):
        from model_tools import handle_function_call
        observed.append(json.loads(handle_function_call('terminal',
            {'command': command, 'background': True}, task_id=task_id)))
        return {'final_response': 'terminal result collected', 'messages': []}
    monkeypatch.setattr(terminal_tool, '_acquire_env', held)
    _model(monkeypatch, model)
    try:
        _job, receipt = await _fire(authority)
        assert await asyncio.to_thread(waiting.wait, 15)
        assert await operation(authority, 'cancel', receipt) == {'ok': True}
        release.set()
        await _settled(authority, receipt)
        assert observed[0].get('code') == 'task_authority_revoked' or 'stopped' in observed[0].get('error', '').lower(), observed
        assert not (tree / 'ready').exists() and not registry.list_sessions()
    finally:
        release.set()
        (tree / 'release').touch()
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')


@pytest.mark.platforms('linux')
@pytest.mark.asyncio
async def test_cancel_observer_disconnect_keeps_cleanup_owned_and_preserves_new_fire(tmp_path, monkeypatch):
    from gateway.session_cron import operation
    from gateway.session_runtime_workers import uncounted_runtime_work

    runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    bodies = [_body(tmp_path, name) for name in ('first', 'later')]
    observed, entering, release = [], threading.Event(), threading.Event()
    def model(_job, task_id):
        observed.append(_start(bodies[len(observed)][1], task_id))
        return {'final_response': 'service running', 'messages': []}
    _model(monkeypatch, model)
    stopper = None
    try:
        _job, first = await _fire(authority)
        await _settled(authority, first)
        await async_wait_until((bodies[0][0] / 'ready').exists, timeout=10, what='original scheduled body')
        first_process = registry.get(observed[0][1]['session_id'])
        kill = registry.kill_process
        def held(process_id, *args, **kwargs):
            if process_id == first_process.id:
                entering.set()
                assert release.wait(30)
            return kill(process_id, *args, **kwargs)
        monkeypatch.setattr(registry, 'kill_process', held)
        stopper = asyncio.create_task(operation(authority, 'cancel', first))
        assert await asyncio.to_thread(entering.wait, 10)
        tracked = [task for task in authority._mutation_tasks if not task.done()]
        assert tracked and uncounted_runtime_work(runner) > 0
        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        _job, later = await _fire(authority)
        await _settled(authority, later)
        await async_wait_until((bodies[1][0] / 'ready').exists, timeout=10, what='new authorized fire during old cleanup')
        later_process = registry.get(observed[1][1]['session_id'])
        release.set()
        await async_wait_until(lambda: all(task.done() for task in tracked), timeout=15, what='actual tracked cancellation cleanup')
        assert first_process.exited and not _alive(int((bodies[0][0] / 'pid').read_text()))
        assert not later_process.exited and _alive(int((bodies[1][0] / 'pid').read_text()))
        for tree, _command in bodies:
            (tree / 'release').touch()
        assert await asyncio.to_thread(later_process._completion_event.wait, 10)
        assert not (bodies[0][0] / 'effect').exists() and (bodies[1][0] / 'effect').read_text() == 'finished'
    finally:
        release.set()
        if stopper is not None and not stopper.done():
            stopper.cancel()
        for tree, _command in bodies:
            (tree / 'release').touch()
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')


@pytest.mark.platforms('linux')
@pytest.mark.asyncio
async def test_cold_cron_scope_remains_unknown_without_reconstructing_its_grant(tmp_path, monkeypatch):
    from pathlib import Path
    import hermes_state
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_cron import operation
    from gateway.session_local_recovery import restore_local_session
    from gateway.session_runtime_workers import command_unknowns, uncounted_runtime_work
    from hermes_state_runtime import RuntimeStoreError
    from tools import process_registry

    # Conftest redirects an already-imported SessionDB default independently of
    # this fixture's home. A real owner uses the same home for DB and process scope.
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', tmp_path / 'home' / 'state.db')
    _runner, authority, original_registry = await _runtime(tmp_path, monkeypatch)
    assert Path(authority.db.db_path).parent == Path(authority.profile_id)
    tree, command = _body(tmp_path, 'body')
    other_tree, other_command = _body(tmp_path, 'other')
    observed = []
    def model(_job, task_id):
        observed.append(_start(command if not observed else other_command, task_id))
        return {'final_response': 'service running', 'messages': []}
    _model(monkeypatch, model)
    try:
        job, receipt = await _fire(authority)
        await _settled(authority, receipt)
        _other_job, other_receipt = await _fire(authority)
        await _settled(authority, other_receipt)
        await async_wait_until((tree / 'ready').exists, timeout=10, what='actual original cron body')
        await async_wait_until((other_tree / 'ready').exists, timeout=10, what='actual other cron body')
        original = original_registry.get(observed[0][1]['session_id'])
        pid = int((tree / 'pid').read_text())
        successor_runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'successor-sessions'))
        successor_runner.session_store._db = successor_runner._session_db = authority.db
        successor = await initialize_session_authority(successor_runner, profile_id=authority.profile_id,
            instance_id='successor', db=authority.db)
        restore_local_session(successor, receipt['session_id'])
        restore_local_session(successor, other_receipt['session_id'])
        cold_registry = process_registry.ProcessRegistry()
        monkeypatch.setattr(process_registry, 'process_registry', cold_registry)
        from gateway.session_authorities import owner_scope
        with owner_scope(successor):
            assert cold_registry.recover_from_checkpoint() == 2
        cold = cold_registry.get(original.id)
        assert cold.detached and cold.reconciliation_required and not cold.exited and _alive(pid)
        assert getattr(cold, 'approval_task_reservation', None) is None
        route = successor.sessions[receipt['session_id']].route
        assert command_unknowns(successor, route) == [cold]
        other = cold_registry.get(observed[1][1]['session_id'])
        assert command_unknowns(successor, successor.sessions[other_receipt['session_id']].route) == [other]
        from gateway.session_ancillary import read
        from gateway.session_contract import SessionRef
        from gateway.session_cron import _actor
        view = await read(SimpleNamespace(authority=successor, actor=_actor(successor, None)),
            SessionRef(successor.profile_id, receipt['session_id']), {'session_id': receipt['session_id']}, kind='processes')
        assert [record['session_id'] for record in view['processes']] == [cold.id]
        assert view['processes'][0]['status'] == 'unknown' and view['processes'][0]['reconciliation_required']
        assert uncounted_runtime_work(successor_runner) > 0
        with pytest.raises(RuntimeStoreError) as caught:
            await operation(successor, 'cancel', receipt)
        assert caught.value.reason == 'unknown_execution' and _alive(pid)
        replay = await operation(successor, 'submit', {'job_id': job['id'], 'request_id': 'native-fire', 'extra_prompt': None})
        assert replay == receipt
        await async_wait_until(lambda: successor.sessions[receipt['session_id']].task is None
            or successor.sessions[receipt['session_id']].task.done(), timeout=10, what='same native fire retry reconciliation')
        assert len(observed) == 2 and len((tree / 'starts').read_text().splitlines()) == 1
        assert not cold.exited and cold.reconciliation_required and not (tree / 'effect').exists()
    finally:
        (tree / 'release').touch()
        (other_tree / 'release').touch()
        await asyncio.to_thread(original_registry.kill_all, source='test-cleanup')
