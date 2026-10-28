"""Native Stop cancellation outlives bounded process output retention."""
import asyncio
from dataclasses import replace
import os
import shlex
import sys
import threading
import time

import pytest

from gateway.platforms.event import MessageEvent
from gateway.session_authority import initialize_session_authority, SessionAuthority
from hermes_state_runtime import list_session_admissions
from tests.gateway.test_automation_compression_media import _admit_human
from tests.gateway.test_native_stop_completion_delivery import owner_with_completion
from tests.gateway.test_native_stop_reconciliation import caller, stop


pytestmark = pytest.mark.platforms('linux')  # Actual local cleanup uses the native Linux job scope.


def expire_result(results, own, mode):
    if mode == 'cap':
        for index in range(results.MAX_RETAINED_RESULTS + 1):
            # Cache pressure is unrelated output history, not copied launch
            # authority or a new owned process requiring a scope receipt.
            # A history-only clone is still stored and counts toward the cap,
            # but has no original publisher that could certify a complete result.
            assert not results.save_completed_result(replace(own, id=f'proc_retention{index:03}',
                owner_task_id='retention-history', task_id='retention-history', parent_session_id='retention-history',
                session_key='retention-history', pid=None, host_start_time=None, native_job_scope=False,
                notify_on_complete=False, completion_consumed=False))
    else:
        from hermes_constants import get_hermes_home
        path = get_hermes_home() / 'logs' / 'process-results' / f'{own.id}.json'
        old = time.time() - results.RESULT_RETENTION_SECONDS - 1
        os.utime(path, (old, old))
    assert own.id not in {path.stem for path in results._result_paths()}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['cap', 'age'])
@pytest.mark.parametrize('delivery', ['batch', 'direct'])
async def test_stop_stays_cancelled_after_output_expiry_and_native_owner_reload(tmp_path, monkeypatch, mode, delivery):
    from tools import process_registry as module, process_registry_results as results
    schedule = SessionAuthority._schedule
    runner, _adapter, authority, source, ref, actor, _registry, own, event = await owner_with_completion(tmp_path, monkeypatch)
    try:
        original = own.output_buffer, own.exit_code
        assert 'result' in await stop(caller(authority, actor), ref)
        expire_result(results, own, mode)
        registry = module.ProcessRegistry()
        monkeypatch.setattr(module, 'process_registry', registry)
        previous_epoch = authority.epoch
        authority = await initialize_session_authority(runner, profile_id=authority.profile_id,
            instance_id='retention-reloaded', db=authority.db)
        assert authority.epoch > previous_epoch
        await authority.resolve(actor, ref)
        assert not registry.is_completion_consumed(own.id), 'bounded output cache must actually have expired'
        deliver = (runner._enqueue_process_completion_notification if delivery == 'batch'
                   else runner._deliver_completion_notification)
        assert await deliver(own.output_buffer, event) is None
        rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
        controls = [row for row in rows if row['principal_id'] == 'native-process-stop']
        assert len(controls) == 1
        assert (controls[0]['status'], controls[0]['outcome']) == ('terminal', 'cancelled')
        assert set(controls[0]['payload']) == {'process_completion', 'route'}
        assert controls[0]['payload']['process_completion'] == controls[0]['request_id']
        assert not any(row['principal_id'].startswith('automation:') for row in rows)
        assert original == (own.output_buffer, own.exit_code)
        # A later real native job and ordinary human input remain eligible.
        fresh = registry.spawn_local('exec ' + shlex.join([sys.executable, '-c', 'print("later valid output")']),
            cwd=str(tmp_path), task_id=ref.session_id, owner_task_id=ref.session_id, session_key=event['session_key'])
        assert await asyncio.to_thread(fresh._completion_event.wait, 15)
        from hermes_constants import get_hermes_home
        (get_hermes_home() / 'logs' / 'process-results' / f'{fresh.id}.json').unlink()
        monkeypatch.setattr(module, 'process_registry', module.ProcessRegistry())
        fresh_event = {**event, 'session_id': fresh.id, 'started_at': fresh.started_at,
                       'output': fresh.output_buffer, 'command': fresh.command}
        assert await runner._enqueue_process_completion_notification(fresh.output_buffer, fresh_event) is True
        await _admit_human(runner, authority, MessageEvent(text='continue', source=source, message_id='later-human'))
        executed = []
        async def execute(owner, target, row):
            executed.append(row)
            return 'fixture answer'
        monkeypatch.setattr('gateway.session_finite.execute_finite_admission', execute)
        monkeypatch.setattr(SessionAuthority, '_schedule', schedule)
        authority._schedule(ref)
        await asyncio.wait_for(authority.sessions[ref.session_id].task, 15)
        assert len(executed) == 2
        assert executed[0]['principal_id'].startswith('automation:')
        assert 'later valid output' in executed[0]['payload']['text']
        assert executed[1]['request_id'] == 'later-human'
        assert all(row['principal_id'] != 'native-process-stop' for row in executed)
    finally:
        await runner._cancel_process_completion_batch_tasks()
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['cap', 'age'])
async def test_native_cancel_guards_a_held_admission_write_after_result_expiry(tmp_path, monkeypatch, mode):
    from gateway import session_automation
    from tools import process_registry as module, process_registry_results as results
    runner, _adapter, authority, _source, ref, actor, _registry, own, event = await owner_with_completion(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    admit = session_automation.admit_session_input
    def held(*args, **kwargs):
        if kwargs['principal_id'].startswith('automation:'):
            entered.set()
            assert release.wait(15)
        return admit(*args, **kwargs)
    monkeypatch.setattr(session_automation, 'admit_session_input', held)
    task = asyncio.create_task(runner._deliver_completion_notification(own.output_buffer, event))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        assert 'result' in await stop(caller(authority, actor), ref)
        expire_result(results, own, mode)
        registry = module.ProcessRegistry()
        monkeypatch.setattr(module, 'process_registry', registry)
        assert not registry.is_completion_consumed(own.id)
        release.set()
        assert await asyncio.wait_for(task, 15) is False
        rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
        assert not any(row['principal_id'].startswith('automation:') for row in rows)
        assert any(row['principal_id'] == 'native-process-stop' and row['status'] == 'terminal' for row in rows)
    finally:
        release.set()
        await task
        await runner._cancel_process_completion_batch_tasks()
        authority.db.close()


@pytest.mark.asyncio
async def test_cancel_receipt_write_failure_keeps_native_stop_fenced_until_retry(tmp_path, monkeypatch):
    from gateway.session_ancillary import process_stop_pending
    runner, _adapter, authority, _source, ref, actor, _registry, own, _event = await owner_with_completion(tmp_path, monkeypatch)
    from hermes_state_runtime import _json
    def refused(payload):
        if 'process_completion' in payload:
            raise PermissionError('native control receipt write refused')
        return _json(payload)
    monkeypatch.setattr('hermes_state_runtime._json', refused)
    try:
        response = await stop(caller(authority, actor), ref)
        assert response['error']['data']['reason'] == 'process_stop_unknown'
        assert process_stop_pending(authority.db, ref.session_id)
        assert not any(row['principal_id'] == 'native-process-stop' for row in
            list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False))
        monkeypatch.setattr('hermes_state_runtime._json', _json)
        assert 'result' in await stop(caller(authority, actor), ref)
        assert process_stop_pending(authority.db, ref.session_id) is None
        assert own.exited
    finally:
        monkeypatch.setattr('hermes_state_runtime._json', _json)
        await runner._cancel_process_completion_batch_tasks()
        authority.db.close()


@pytest.mark.asyncio
async def test_native_cancel_receipt_survives_later_terminal_inputs_in_active_session(tmp_path, monkeypatch):
    from tools import process_registry as module, process_registry_results as results
    from hermes_state_runtime import get_session_admission
    runner, _adapter, authority, source, ref, actor, _registry, own, event = await owner_with_completion(tmp_path, monkeypatch)
    try:
        assert 'result' in await stop(caller(authority, actor), ref)
        rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
        control = next(row for row in rows if row['principal_id'] == 'native-process-stop')
        for index in range(results.MAX_RETAINED_RESULTS + 1):
            receipt = await _admit_human(runner, authority, MessageEvent(text='later native input',
                source=source, message_id=f'later-terminal-{index}'))
            await authority.cancel_queued(actor, ref, receipt.admission_id)
        expire_result(results, own, 'cap')
        monkeypatch.setattr(module, 'process_registry', module.ProcessRegistry())
        authority = await initialize_session_authority(runner, profile_id=authority.profile_id,
            instance_id='terminal-input-pressure-reload', db=authority.db)
        await authority.resolve(actor, ref)
        retained = get_session_admission(authority.db, admission_id=control['admission_id'])
        assert (retained['status'], retained['outcome']) == ('terminal', 'cancelled')
        assert await runner._enqueue_process_completion_notification(own.output_buffer, event) is None
        rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
        assert len(rows) == results.MAX_RETAINED_RESULTS + 3
        assert not any(row['principal_id'].startswith('automation:') for row in rows)
    finally:
        await runner._cancel_process_completion_batch_tasks()
        authority.db.close()


@pytest.mark.asyncio
async def test_expired_cancelled_completion_cannot_bypass_native_stop_with_unknown_incarnation(tmp_path, monkeypatch):
    from tools import process_registry as module, process_registry_results as results
    runner, _adapter, authority, _source, ref, actor, _registry, own, event = await owner_with_completion(tmp_path, monkeypatch)
    try:
        assert 'result' in await stop(caller(authority, actor), ref)
        expire_result(results, own, 'cap')
        monkeypatch.setattr(module, 'process_registry', module.ProcessRegistry())
        authority = await initialize_session_authority(runner, profile_id=authority.profile_id,
            instance_id='unknown-incarnation-reload', db=authority.db)
        await authority.resolve(actor, ref)
        for incarnation in (None, True, 'unknown', {}, [], float('nan'), float('inf'), 0, 0.0, -1):
            uncertain = {**event, 'started_at': incarnation}
            assert await runner._deliver_completion_notification(own.output_buffer, uncertain) is None
        missing = dict(event)
        missing.pop('started_at')
        assert await runner._enqueue_process_completion_notification(own.output_buffer, missing) is None
        assert not any(row['principal_id'].startswith('automation:') for row in
            list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False))
    finally:
        await runner._cancel_process_completion_batch_tasks()
        authority.db.close()
