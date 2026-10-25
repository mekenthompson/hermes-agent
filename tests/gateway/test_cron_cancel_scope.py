"""Cron cancellation keeps exact admission identity and private process ownership."""
from dataclasses import replace

import pytest

from tests.gateway.test_native_cron_task_authority import _runtime, _fire, _settled


@pytest.mark.asyncio
async def test_queued_cron_cancel_winner_admits_no_effects_and_is_repeatable(tmp_path, monkeypatch):
    from gateway.session_cron import operation
    from hermes_state_runtime import get_session_admission
    _runner, authority, registry = await _runtime(tmp_path, monkeypatch, paused=True)
    _job, receipt = await _fire(authority)
    assert await operation(authority, 'cancel', receipt) == {'ok': True}
    assert await operation(authority, 'cancel', receipt) == {'ok': True}
    row = get_session_admission(authority.db, admission_id=receipt['admission_id'])
    assert (row['status'], row['outcome'], row['generation']) == ('terminal', 'cancelled', None)
    assert not authority.pending_stops and not getattr(authority, '_cron_cancellations', {})
    assert not registry.list_sessions()


@pytest.mark.asyncio
async def test_queued_to_claimed_cron_cancel_latches_exact_original_generation(tmp_path, monkeypatch):
    from gateway.session_cron import operation
    from hermes_state_runtime import claim_session_input, get_session_admission
    _runner, authority, _registry = await _runtime(tmp_path, monkeypatch, paused=True)
    _job, receipt = await _fire(authority)
    cancel = authority.cancel_queued
    async def claimed(actor, ref, admission_id):
        claim_session_input(authority.db, epoch=authority.epoch, session_id=ref.session_id)
        return await cancel(actor, ref, admission_id)
    monkeypatch.setattr(authority, 'cancel_queued', claimed)
    assert await operation(authority, 'cancel', receipt) == {'ok': True}
    row = get_session_admission(authority.db, admission_id=receipt['admission_id'])
    assert row['status'] == 'started' and row['generation'] == 1
    assert authority.pending_stops[receipt['session_id']] == 1
    assert authority._cron_cancellations[row['admission_id']].is_set()


@pytest.mark.asyncio
async def test_replacement_owner_cannot_claim_old_cron_effects_stopped_from_empty_registry(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_cron import operation
    from hermes_state_runtime import RuntimeStoreError, claim_session_input, settle_session_input
    _runner, original, registry = await _runtime(tmp_path, monkeypatch, paused=True)
    _job, receipt = await _fire(original)
    row = claim_session_input(original.db, epoch=original.epoch, session_id=receipt['session_id'])
    settle_session_input(original.db, epoch=original.epoch, admission_id=row['admission_id'],
                         generation=row['generation'], outcome='completed')
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'replacement-sessions'))
    runner.session_store._db = runner._session_db = original.db
    successor = await initialize_session_authority(runner, profile_id=original.profile_id,
                                                   instance_id='replacement', db=original.db)
    assert not registry.list_sessions()
    with pytest.raises(RuntimeStoreError, match='unknown_execution'):
        await operation(successor, 'cancel', receipt)
    assert not getattr(successor, '_cron_cancellations', {})


async def _reserved_fire(authority, monkeypatch, reservations):
    from cron import scheduler
    from tools.approval_context import reserve_background_approval_task
    def execute(job, **kwargs):
        reservations.append(reserve_background_approval_task())
        return True, 'doc', 'done', None
    monkeypatch.setattr(scheduler, 'run_job', execute)
    _job, receipt = await _fire(authority)
    await _settled(authority, receipt)
    return receipt, reservations[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lease', 'session', 'profile', 'missing'])
async def test_stateless_process_ownership_requires_original_private_lease_and_exact_scope(
        tmp_path, monkeypatch, fault):
    from gateway.session_ancillary import _owned_locked
    from gateway.session_runtime_workers import command_unknowns
    _runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    reservations = []
    receipt, reservation = await _reserved_fire(authority, monkeypatch, reservations)
    live = authority.sessions[receipt['session_id']]
    original = reservation.evidence
    process = registry._new_session('not launched', 'private-task', 'private-task', '', str(tmp_path))
    process.approval_task_reservation = reservation
    process.reconciliation_required = 'Unknown original native process scope'
    if fault == 'lease':
        _other, process.approval_task_reservation = await _reserved_fire(authority, monkeypatch, reservations)
        process.approval_task_reservation.evidence = replace(original)
    if fault in {'session', 'profile'}:
        updates = {'session': {'session': 'another-session'}, 'profile': {'profile': 'another-profile'}}
        reservation.evidence = replace(original, **updates[fault])
    if fault == 'missing':
        process.approval_task_reservation = None
    registry._running[process.id] = process
    try:
        with registry._lock:
            owned = _owned_locked(registry, {live.route}, {receipt['session_id']}, authority, live)
        assert (process in owned) is (fault is None)
        assert (process in command_unknowns(authority, live.route)) is (fault is None)
    finally:
        registry._running.pop(process.id)
        reservation.evidence = original
        for item in reservations:
            item.release()


@pytest.mark.asyncio
async def test_missing_original_cron_history_cannot_clear_cold_process_uncertainty(tmp_path, monkeypatch):
    from gateway.session_runtime_workers import command_unknowns
    from tools.terminal_tool_background import _record_native_origin
    from hermes_state_runtime import RuntimeStoreError
    _runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    reservations = []
    receipt, reservation = await _reserved_fire(authority, monkeypatch, reservations)
    process = registry._new_session('not launched', 'private-task', 'private-task', '', str(tmp_path))
    _record_native_origin(process, reservation)
    process.detached = True
    process.reconciliation_required = 'Unknown original scope'
    registry._running[process.id] = process
    authority.db._execute_write(lambda conn: conn.execute(
        'DELETE FROM session_admissions WHERE admission_id=?', (receipt['admission_id'],)))
    try:
        with pytest.raises(RuntimeStoreError, match='unknown_execution'):
            command_unknowns(authority, authority.sessions[receipt['session_id']].route)
        assert process.reconciliation_required and getattr(process, 'approval_task_reservation', None) is None
    finally:
        registry._running.pop(process.id)
        reservation.release()
