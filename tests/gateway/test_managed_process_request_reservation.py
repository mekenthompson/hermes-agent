"""A worker receipt reserves one descriptor; cached results never recreate execution."""
import json

import pytest

from gateway.session_worker_process import claim_request, materialize_descriptor, request_digest, reserve_request
from hermes_state import SessionDB
from hermes_state_runtime import RuntimeStoreError, begin_runtime_epoch, register_worker_execution
from tools.process_registry import ProcessRegistry


@pytest.fixture
def reservation(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('physical', source='cli')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        scope = dict(epoch=epoch, execution_id='compute', session_id='physical', generation=0)
        register_worker_execution(db, **scope, kind='compute', adoption_secret='private')
        yield db, scope


def _request():
    arguments = {'command': 'printf once', 'background': True}
    return {'tool': 'terminal', 'digest': request_digest('terminal', arguments)}, arguments


def test_exact_receipt_retry_has_host_derived_freshness_and_same_descriptor(reservation, tmp_path):
    db, scope = reservation
    request, arguments = _request()
    first, fresh = reserve_request(db, scope, 1, request)
    assert fresh and 'fresh' not in first and 'created' not in first
    registry = ProcessRegistry()
    descriptor = materialize_descriptor(registry, first, fresh, task_id='physical', route='route', cwd=str(tmp_path))
    second, fresh = reserve_request(db, scope, 1, request)
    assert second == first and not fresh
    assert materialize_descriptor(registry, second, fresh, task_id='physical', route='route', cwd=str(tmp_path)) is descriptor
    payload = {'tool': 'terminal', 'arguments': arguments}
    claimed, fresh = claim_request(db, scope, 1, payload)
    assert fresh and claimed['resource_id'] == first['resource_id']
    claimed, fresh = claim_request(db, scope, 1, payload)
    assert not fresh and claimed['issued']
    with db._read_ctx() as conn:
        saved = json.loads(conn.execute('SELECT result_json FROM worker_receipts').fetchone()[0])
    assert saved['issued'] and 'created' not in saved and 'fresh' not in saved
    assert descriptor.pid is None and descriptor._local_launch_pending


@pytest.mark.parametrize('cold_checkpoint', [False, True])
def test_missing_or_cold_descriptor_is_never_recreated_from_cached_receipt(reservation, tmp_path, cold_checkpoint):
    db, scope = reservation
    request, _arguments = _request()
    saved, fresh = reserve_request(db, scope, 1, request)
    if cold_checkpoint:
        registry = ProcessRegistry()
        materialize_descriptor(registry, saved, fresh, task_id='physical', route='route', cwd=str(tmp_path))
    replay, fresh = reserve_request(db, scope, 1, request)
    assert not fresh
    restarted = ProcessRegistry()
    if cold_checkpoint:
        assert restarted.recover_from_checkpoint() == 1
        assert restarted.get(replay['resource_id']).detached
    with pytest.raises(RuntimeStoreError, match='unknown_execution'):
        materialize_descriptor(restarted, replay, fresh, task_id='physical', route='route', cwd=str(tmp_path))
    assert cold_checkpoint or not restarted._running


def test_reserved_command_digest_cannot_be_changed_before_effect(reservation):
    db, scope = reservation
    request, arguments = _request()
    reserve_request(db, scope, 1, request)
    with pytest.raises(RuntimeStoreError, match='admission_conflict'):
        claim_request(db, scope, 1, {'tool': 'terminal', 'arguments': dict(arguments, command='printf different')})
    _receipt, fresh = claim_request(db, scope, 1, {'tool': 'terminal', 'arguments': arguments})
    assert fresh, 'a refused altered command cannot consume the authorized request'


def test_observed_cwd_retry_cannot_reanchor_a_committed_native_request(reservation):
    db, scope = reservation
    _reserved, arguments = _request()
    reserve_request(db, scope, 1, {'tool': 'terminal', 'digest': request_digest('terminal', arguments, '/original')})
    with pytest.raises(RuntimeStoreError, match='admission_conflict'):
        claim_request(db, scope, 1, {'tool': 'terminal', 'arguments': arguments, 'observed_cwd': '/changed'})
    _receipt, fresh = claim_request(db, scope, 1, {'tool': 'terminal', 'arguments': arguments, 'observed_cwd': '/original'})
    assert fresh
