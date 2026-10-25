"""Supervisor Stop retains the dispatcher attempt's exact native admission identity."""
from contextlib import asynccontextmanager, closing
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
from hermes_cli import gateway_client, gateway_runtime
from hermes_cli.kanban_owner_control import interrupt_owner_run
from hermes_cli.process_identity import process_birth
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
import hermes_state_runtime as runtime


@pytest.fixture
def attempt(tmp_path, monkeypatch):
    home = tmp_path / 'owner'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    with closing(kbc.connect(tmp_path / 'board.db')) as board, SessionDB(db_path=home / 'state.db') as owner:
        # Board creation is explicit, as it is for the real dispatcher.
        kbc.init_db(tmp_path / 'board.db')
        tid = kb.create_task(board, title='bound task', assignee='default')
        task = kb.claim_task(board, tid)
        owner.create_session('original', source='cli')
        epoch = runtime.begin_runtime_epoch(owner, instance_id='owner-instance')
        request = 'kanban:' + json.dumps(['default', tid, task.current_run_id], separators=(',', ':'))
        receipt = runtime.admit_session_input(owner, epoch=epoch, principal_id='human',
            session_id='original', request_id=request, payload={'text': 'execute'})
        runtime.claim_session_input(owner, epoch=epoch, session_id='original')
        with kb.write_txn(board):
            kb._append_event(board, tid, 'owner_admitted', {'db': str(home / 'state.db'),
                'session_id': 'original', 'request_id': request, 'pid': os.getpid(),
                'birth': process_birth(psutil.Process())}, run_id=task.current_run_id)
        yield SimpleNamespace(board=board, owner=owner, task=task, receipt=receipt, home=home, epoch=epoch)


def _endpoint(attempt):
    return gateway_runtime.GatewayEndpoint(profile_id=str(attempt.home), instance_id='owner-instance',
        authority_epoch=attempt.epoch, runtime_protocol=1, api_origin='http://127.0.0.1:1',
        supervisor='none', capabilities=frozenset())


def _control(monkeypatch, attempt, *, refusal=None):
    calls = []
    def discover(home, **kwargs):
        assert home == attempt.home
        return gateway_runtime.GatewayDiscovery(state='ready', endpoint=_endpoint(attempt))
    class Client:
        async def rpc(self, method, **params):
            assert not attempt.board.in_transaction
            assert get_hermes_home() == attempt.home
            calls.append((method, params))
            if refusal:
                raise gateway_client.GatewayRPCError(refusal)
            return {'ref': {'session_id': params['session_id']},
                    'execution_generation': params['execution_generation'], 'execution_state': 'running'}
    @asynccontextmanager
    async def connect(*, endpoint):
        assert endpoint == _endpoint(attempt)
        yield Client()
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', discover)
    monkeypatch.setattr(gateway_client, 'connect_gateway', connect)
    monkeypatch.setattr(gateway_runtime, 'ensure_gateway_runtime',
        lambda *a, **kw: pytest.fail('Stop must never start an owner'))
    return calls


def _events(attempt):
    return [json.loads(row['payload']) for row in attempt.board.execute(
        "SELECT payload FROM task_events WHERE kind='native_owner_stop' AND task_id=? "
        "AND json_extract(payload,'$.status')!='pending' ORDER BY id",
        (attempt.task.id,))]


def test_supervisor_block_interrupts_original_request_generation_after_commit(attempt, monkeypatch):
    calls = _control(monkeypatch, attempt)
    assert kb.block_task(attempt.board, attempt.task.id, kind='needs_input')
    assert calls == [('session.interrupt', {'_timeout': 25, 'session_id': 'original', 'execution_generation': 1})]
    assert _events(attempt)[0]['status'] == 'confirmed'
    assert _events(attempt)[0]['execution_state'] == 'running'
    assert _events(attempt)[0]['confirmation'] == 'effect_scope_cleaned'
    # A concurrent Stop losing its reply cannot undo already-confirmed cleanup.
    with kb.write_txn(attempt.board):
        kb._append_event(attempt.board, attempt.task.id, 'native_owner_stop',
            {'status': 'unknown', 'reason': 'owner_unavailable_or_replaced'},
            run_id=attempt.task.current_run_id)
    runtime.begin_runtime_epoch(attempt.owner, instance_id='replacement-owner')
    assert kb.halt_blocked_worker(attempt.board, attempt.task.current_run_id)
    assert len(calls) == 1  # confirmed old Stop is replayed without touching the new owner


def test_delayed_old_stop_does_not_retry_a_newer_generation(attempt, monkeypatch):
    calls = _control(monkeypatch, attempt, refusal='stale_generation')
    # The session has moved on; the exact old producer admission remains generation one.
    runtime.settle_session_input(attempt.owner, epoch=attempt.epoch,
        admission_id=attempt.receipt['admission_id'], generation=1, outcome='completed')
    runtime.admit_session_input(attempt.owner, epoch=attempt.epoch, principal_id='human',
        session_id='original', request_id='replacement', payload={'text': 'later authorized work'})
    runtime.claim_session_input(attempt.owner, epoch=attempt.epoch, session_id='original')
    assert kb.block_task(attempt.board, attempt.task.id, kind='needs_input')
    assert [params['execution_generation'] for _, params in calls] == [1]
    assert _events(attempt)[0]['reason'] == 'stale_generation'
    assert interrupt_owner_run(attempt.board, attempt.task.current_run_id) is False
    assert [params['execution_generation'] for _, params in calls] == [1, 1]


def test_unavailable_owner_is_unknown_without_start_or_fallback(attempt, monkeypatch):
    calls = _control(monkeypatch, attempt)
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', lambda *a, **kw:
        gateway_runtime.GatewayDiscovery(state='absent', reason_code='owner_absent'))
    assert kb.block_task(attempt.board, attempt.task.id, kind='needs_input')
    assert calls == []
    assert _events(attempt)[0]['status'] == 'unknown'
    assert interrupt_owner_run(attempt.board, attempt.task.current_run_id) is False
    assert kb.unblock_task(attempt.board, attempt.task.id) is False
    # A stale operator path clearing current_run_id cannot erase the original uncertainty.
    with kb.write_txn(attempt.board):
        attempt.board.execute("UPDATE tasks SET status='ready',current_run_id=NULL WHERE id=?",
                              (attempt.task.id,))
    assert kb.claim_task(attempt.board, attempt.task.id) is None


def test_arbitrary_rpc_error_details_are_not_saved_or_logged(attempt, monkeypatch, caplog):
    detail = 'private transport command detail'
    _control(monkeypatch, attempt, refusal=detail)
    assert kb.block_task(attempt.board, attempt.task.id, kind='needs_input')
    assert _events(attempt)[0]['reason'] == 'owner_control_unconfirmed'
    assert detail not in json.dumps(_events(attempt))
    assert detail not in caplog.text


def test_supported_exact_run_stop_reinspection_releases_unknown_fence(attempt, monkeypatch):
    _control(monkeypatch, attempt, refusal='unknown_execution')
    assert kb.block_task(attempt.board, attempt.task.id, kind='needs_input')
    assert kb.unblock_task(attempt.board, attempt.task.id) is False
    calls = _control(monkeypatch, attempt)
    assert kb.halt_blocked_worker(attempt.board, attempt.task.current_run_id) is True
    assert [params['execution_generation'] for _, params in calls] == [1]
    assert kb.unblock_task(attempt.board, attempt.task.id) is True
    replacement = kb.claim_task(attempt.board, attempt.task.id)
    assert replacement.current_run_id != attempt.task.current_run_id


def test_unknown_stop_preserves_workspace_even_after_worker_identity_clears(attempt, monkeypatch, tmp_path):
    _control(monkeypatch, attempt, refusal='unknown_execution')
    scratch = kb.workspaces_root() / attempt.task.id
    scratch.mkdir(parents=True)
    (scratch / 'checkpoint').write_text('preserve original effects')
    with kb.write_txn(attempt.board):
        attempt.board.execute("UPDATE tasks SET workspace_kind='scratch',workspace_path=? WHERE id=?",
                              (str(scratch), attempt.task.id))
    assert kb.block_task(attempt.board, attempt.task.id, kind='needs_input')
    assert kb.archive_task(attempt.board, attempt.task.id)
    kb._cleanup_workspace(attempt.board, attempt.task.id)
    assert (scratch / 'checkpoint').read_text() == 'preserve original effects'


def test_worker_self_block_preserves_ordinary_release_and_skips_native_stop(attempt, monkeypatch):
    calls = _control(monkeypatch, attempt)
    assert kb.block_task(attempt.board, attempt.task.id, kind='needs_input',
                         expected_run_id=attempt.task.current_run_id)
    assert calls == []
    assert _events(attempt) == []


def test_manual_reclaim_unknown_retains_original_claim_and_delete_evidence(attempt, monkeypatch):
    _control(monkeypatch, attempt, refusal='unknown_execution')
    assert kb.reclaim_task(attempt.board, attempt.task.id) is False
    task = kb.get_task(attempt.board, attempt.task.id)
    assert task.status == 'running' and task.current_run_id == attempt.task.current_run_id
    assert _events(attempt)[0]['status'] == 'unknown'
    with pytest.raises(ValueError, match='stop/reconcile'):
        kb.delete_task(attempt.board, attempt.task.id)
    assert kb.get_task(attempt.board, attempt.task.id) is not None


def test_owner_replacement_is_not_authority_to_stop_old_attempt(attempt, monkeypatch):
    calls = _control(monkeypatch, attempt)
    runtime.begin_runtime_epoch(attempt.owner, instance_id='replacement-owner')
    assert kb.block_task(attempt.board, attempt.task.id, kind='needs_input')
    assert calls == []
    assert _events(attempt)[0]['status'] == 'unknown'


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('path', ['ttl', 'stale', 'timeout', 'manual', 'archive'])
def test_existing_termination_paths_stop_native_scope_before_submitter(attempt, monkeypatch, path):
    calls = _control(monkeypatch, attempt)
    worker = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'],
                              stdin=subprocess.PIPE)
    try:
        dispatch._set_worker_pid(attempt.board, attempt.task.id, worker.pid)
        with kb.write_txn(attempt.board):
            attempt.board.execute('UPDATE tasks SET claim_expires=1,last_heartbeat_at=1,'
                                  'started_at=1,max_runtime_seconds=1 WHERE id=?', (attempt.task.id,))
            attempt.board.execute('UPDATE task_runs SET started_at=1 WHERE id=?',
                                  (attempt.task.current_run_id,))
        actions = {'ttl': lambda: kb.release_stale_claims(attempt.board),
            'stale': lambda: dispatch.detect_stale_running(attempt.board, stale_timeout_seconds=1),
            'timeout': lambda: dispatch.enforce_max_runtime(attempt.board),
            'manual': lambda: kb.reclaim_task(attempt.board, attempt.task.id),
            'archive': lambda: kb.archive_task(attempt.board, attempt.task.id)}
        assert actions[path]()
        assert worker.wait(timeout=10) != 0
        assert len(calls) == 1
        assert calls[0][1]['execution_generation'] == 1
        assert _events(attempt)[0]['status'] == 'confirmed'
        assert kb.get_task(attempt.board, attempt.task.id).status == ('archived' if path == 'archive' else 'ready')
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait()
        worker.stdin.close()
