"""Supervisor Stop cleans original native scopes even when task state is already blocked."""
import json
import os

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_owner_recovery import owner_stop_unresolved
from hermes_cli.process_identity import process_birth
import hermes_state_runtime as runtime
from tests.hermes_cli.test_kanban_owner_stop import attempt as _attempt_fixture, _control, _events

attempt = _attempt_fixture


def _self_block(attempt):
    assert kb.block_task(attempt.board, attempt.task.id, kind='needs_input',
                         expected_run_id=attempt.task.current_run_id)


def _next_owner_run(attempt):
    assert kb.unblock_task(attempt.board, attempt.task.id)
    task = kb.claim_task(attempt.board, attempt.task.id)
    attempt.owner.create_session('next', source='cli')
    request = 'kanban:' + json.dumps(['default', task.id, task.current_run_id], separators=(',', ':'))
    runtime.admit_session_input(attempt.owner, epoch=attempt.epoch, principal_id='human',
        session_id='next', request_id=request, payload={'text': 'next authorized run'})
    runtime.claim_session_input(attempt.owner, epoch=attempt.epoch, session_id='next')
    with kb.write_txn(attempt.board):
        kb._append_event(attempt.board, task.id, 'owner_admitted', {'db': str(attempt.home / 'state.db'),
            'session_id': 'next', 'request_id': request, 'pid': os.getpid(),
            'birth': process_birth(psutil.Process())}, run_id=task.current_run_id)
    return task


def _receipts(attempt):
    return attempt.board.execute("SELECT id,payload FROM task_events WHERE task_id=? "
                                 "AND kind='lifecycle_request' ORDER BY id", (attempt.task.id,)).fetchall()


def test_supervisor_stop_after_self_block_fences_before_cleanup_without_status_or_hook_change(attempt, monkeypatch):
    _self_block(attempt)
    calls = _control(monkeypatch, attempt)
    hooks, runs = [], kb.list_runs(attempt.board, attempt.task.id)
    monkeypatch.setattr(kb, '_fire_task_hook', lambda *args, **kw: hooks.append(args))
    original = kb.halt_blocked_worker
    def halt(conn, run_id):
        assert not conn.in_transaction
        assert owner_stop_unresolved(conn, attempt.task.id)
        assert kb.unblock_task(conn, attempt.task.id) is False
        return original(conn, run_id)
    monkeypatch.setattr(kb, 'halt_blocked_worker', halt)
    assert kb.block_task(attempt.board, attempt.task.id, kind='needs_input', idempotency_key='stop') is False
    assert [params['session_id'] for _, params in calls] == ['original']
    assert kb.list_runs(attempt.board, attempt.task.id) == runs
    assert hooks == []
    assert json.loads(_receipts(attempt)[0]['payload'])['stop_run_ids'] == [attempt.task.current_run_id]
    assert owner_stop_unresolved(attempt.board, attempt.task.id) is False


@pytest.mark.parametrize('already_blocked', [False, True])
def test_same_key_retries_original_unknown_cleanup_with_stable_bool_and_one_hook(attempt, monkeypatch, already_blocked):
    if already_blocked:
        _self_block(attempt)
    _control(monkeypatch, attempt, refusal='unknown_execution')
    hooks = []
    monkeypatch.setattr(kb, '_fire_task_hook', lambda *args, **kw: hooks.append(args))
    params = dict(kind='needs_input', reason='Stop', idempotency_key='stable')
    assert kb.block_task(attempt.board, attempt.task.id, **params) is (not already_blocked)
    assert owner_stop_unresolved(attempt.board, attempt.task.id)
    before = [event for event in kb.list_events(attempt.board, attempt.task.id) if event.kind != 'native_owner_stop']
    calls = _control(monkeypatch, attempt)
    assert kb.block_task(attempt.board, attempt.task.id, **params) is (not already_blocked)
    assert [params['execution_generation'] for _, params in calls] == [1]
    assert [event for event in kb.list_events(attempt.board, attempt.task.id) if event.kind != 'native_owner_stop'] == before
    assert len(hooks) == int(not already_blocked)
    assert len(_receipts(attempt)) == 1
    assert _events(attempt)[-1]['status'] == 'confirmed'
    assert not owner_stop_unresolved(attempt.board, attempt.task.id)


@pytest.mark.parametrize('legacy_receipt', [False, True])
def test_old_key_replay_never_captures_successor_owner_scope(attempt, monkeypatch, legacy_receipt):
    _control(monkeypatch, attempt)
    params = dict(kind='needs_input', idempotency_key='old-stop')
    assert kb.block_task(attempt.board, attempt.task.id, **params)
    if legacy_receipt:
        saved = _receipts(attempt)[0]
        payload = json.loads(saved['payload'])
        del payload['stop_run_ids']
        with kb.write_txn(attempt.board):
            attempt.board.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), saved['id']))
            # Old task-scoped block_requested events can disappear under retention.
            # Original admission markers and the request's own event boundary survive.
            attempt.board.execute("DELETE FROM task_events WHERE task_id=? AND kind='blocked'",
                                  (attempt.task.id,))
    successor = _next_owner_run(attempt)
    calls = _control(monkeypatch, attempt)
    assert kb.block_task(attempt.board, attempt.task.id, **params)
    assert calls == []
    current = kb.get_task(attempt.board, attempt.task.id)
    assert current.status == 'running' and current.current_run_id == successor.current_run_id
    assert not attempt.board.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? "
        "AND kind='native_owner_stop'", (current.id, current.current_run_id)).fetchone()


def test_new_stop_captures_all_prior_persisted_native_task_runs(attempt, monkeypatch):
    _self_block(attempt)
    successor = _next_owner_run(attempt)
    calls = _control(monkeypatch, attempt)
    assert kb.block_task(attempt.board, attempt.task.id, kind='needs_input', idempotency_key='all-scopes')
    assert [params['session_id'] for _, params in calls] == ['original', 'next']
    assert json.loads(_receipts(attempt)[0]['payload'])['stop_run_ids'] == [
        attempt.task.current_run_id, successor.current_run_id]


def test_missing_original_run_is_unknown_and_fenced_without_fabricating_a_run(attempt, monkeypatch):
    _self_block(attempt)
    with kb.write_txn(attempt.board):
        attempt.board.execute('DELETE FROM task_runs WHERE id=?', (attempt.task.current_run_id,))
    calls = _control(monkeypatch, attempt)
    assert kb.block_task(attempt.board, attempt.task.id, kind='needs_input') is False
    assert calls == []
    assert _events(attempt)[-1]['reason'] == 'missing_original_stop_scope'
    assert kb.list_runs(attempt.board, attempt.task.id) == []
    assert owner_stop_unresolved(attempt.board, attempt.task.id)
    assert kb.unblock_task(attempt.board, attempt.task.id) is False


def test_ambiguous_original_owner_is_unknown_without_selecting_a_binding(attempt, monkeypatch):
    row = attempt.board.execute("SELECT payload FROM task_events WHERE kind='owner_admitted'").fetchone()
    binding = dict(json.loads(row['payload']), session_id='ambiguous')
    with kb.write_txn(attempt.board):
        kb._append_event(attempt.board, attempt.task.id, 'owner_admitted', binding,
                         run_id=attempt.task.current_run_id)
    calls = _control(monkeypatch, attempt)
    assert kb.block_task(attempt.board, attempt.task.id, kind='needs_input')
    assert calls == []
    assert _events(attempt)[-1]['status'] == 'unknown'
    assert owner_stop_unresolved(attempt.board, attempt.task.id)
    assert kb.unblock_task(attempt.board, attempt.task.id) is False


def test_unknown_admission_allows_only_original_control_and_requires_known_cleanup(attempt, monkeypatch):
    attempt.owner._execute_write(lambda conn: conn.execute("UPDATE session_admissions SET status='unknown'"))
    calls = _control(monkeypatch, attempt, execution_state='unknown')
    params = dict(kind='needs_input', idempotency_key='unknown-original')
    assert kb.block_task(attempt.board, attempt.task.id, **params)
    assert [params['execution_generation'] for _, params in calls] == [1]
    assert _events(attempt)[-1]['status'] == 'unknown'
    assert owner_stop_unresolved(attempt.board, attempt.task.id)
    runtime.resolve_unknown_session_input(attempt.owner, epoch=attempt.epoch,
        admission_id=attempt.receipt['admission_id'], generation=1)
    calls = _control(monkeypatch, attempt, execution_state='idle')
    assert kb.block_task(attempt.board, attempt.task.id, **params)
    assert [params['execution_generation'] for _, params in calls] == [1]
    assert _events(attempt)[-1]['status'] == 'confirmed'
    assert not owner_stop_unresolved(attempt.board, attempt.task.id)
