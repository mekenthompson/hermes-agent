"""Ordinary retained-submitter reaping does not cancel normally released native services."""
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
from hermes_cli.kanban_owner_control import capture_supervisor_stop, owner_scope_stop_required
from hermes_cli.kanban_owner_recovery import owner_stop_unresolved
from tests.hermes_cli.test_kanban_owner_stop import attempt as _attempt_fixture, _control, _events
from tests.hermes_cli.test_kanban_supervisor_stop import _next_owner_run

attempt = _attempt_fixture


@pytest.fixture
def dead_submitter(attempt):
    proc = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'], stdin=subprocess.PIPE)
    try:
        dispatch._set_worker_pid(attempt.board, attempt.task.id, proc.pid)
        proc.kill()
        proc.wait(timeout=10)
        yield proc.pid
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdin.close()


def _close(attempt, outcome, *, supervisor=None):
    phases = {'completed': 'done', 'review_requested': 'review', 'changes_requested': 'changes_requested',
              'scheduled': 'scheduled', 'blocked': 'blocked'}
    with kb.write_txn(attempt.board):
        kb._end_run(attempt.board, attempt.task.id, outcome=outcome, status=outcome)
        attempt.board.execute('UPDATE tasks SET status=?,worker_pid=NULL,claim_lock=NULL WHERE id=?',
                              (phases.get(outcome, 'ready'), attempt.task.id))
        if outcome == 'blocked' and supervisor is not None:
            kb._append_event(attempt.board, attempt.task.id, 'blocked', {'supervisor_stop': supervisor},
                             run_id=attempt.task.current_run_id)


def _retained(attempt):
    return attempt.board.execute('SELECT worker_pid FROM task_runs WHERE id=?',
                                 (attempt.task.current_run_id,)).fetchone()['worker_pid']


@pytest.mark.parametrize('outcome', ['completed', 'scheduled', 'review_requested', 'changes_requested', 'blocked'])
def test_normal_release_reaps_dead_submitter_without_native_stop(attempt, dead_submitter, monkeypatch, outcome):
    _close(attempt, outcome, supervisor=False)
    calls = _control(monkeypatch, attempt)
    monkeypatch.setattr(dispatch, 'TERMINAL_WORKER_REAP_GRACE_SECONDS', 0)
    assert owner_scope_stop_required(attempt.board, attempt.task.current_run_id) is False
    dispatch.reap_terminal_workers(attempt.board)
    assert _retained(attempt) is None
    assert calls == [] and _events(attempt) == []
    assert not owner_stop_unresolved(attempt.board, attempt.task.id)


@pytest.mark.parametrize('outcome', ['reclaimed', 'stale', 'timed_out', 'crashed', 'gave_up', 'spawn_failed'])
def test_aborted_scope_requires_exact_native_cleanup_before_reaper_clears_evidence(
        attempt, dead_submitter, monkeypatch, outcome):
    _close(attempt, outcome)
    calls = _control(monkeypatch, attempt, refusal='unknown_execution')
    monkeypatch.setattr(dispatch, 'TERMINAL_WORKER_REAP_GRACE_SECONDS', 0)
    assert owner_scope_stop_required(attempt.board, attempt.task.current_run_id)
    dispatch.reap_terminal_workers(attempt.board)
    assert len(calls) == 1 and calls[0][1]['execution_generation'] == 1
    assert _retained(attempt) == dead_submitter
    assert owner_stop_unresolved(attempt.board, attempt.task.id)
    calls = _control(monkeypatch, attempt)
    dispatch.reap_terminal_workers(attempt.board)
    assert len(calls) == 1
    assert _retained(attempt) is None
    assert _events(attempt)[-1]['status'] == 'confirmed'


@pytest.mark.parametrize('intent', ['audit', 'supervisor_block', 'historical_request'])
def test_explicit_stop_on_an_ended_scope_is_still_reconciled(attempt, dead_submitter, monkeypatch, intent):
    _close(attempt, 'blocked', supervisor=intent == 'supervisor_block')
    if intent == 'audit':
        with kb.write_txn(attempt.board):
            capture_supervisor_stop(attempt.board, attempt.task.id)
    elif intent == 'historical_request':
        with kb.write_txn(attempt.board):
            kb._append_event(attempt.board, attempt.task.id, 'block_requested', {'reason': 'Stop'})
    calls = _control(monkeypatch, attempt)
    monkeypatch.setattr(dispatch, 'TERMINAL_WORKER_REAP_GRACE_SECONDS', 0)
    assert owner_stop_unresolved(attempt.board, attempt.task.id)
    assert owner_scope_stop_required(attempt.board, attempt.task.current_run_id)
    dispatch.reap_terminal_workers(attempt.board)
    assert len(calls) == 1
    assert _retained(attempt) is None
    assert _events(attempt)[-1]['status'] == 'confirmed'


def test_missing_old_block_intent_preserves_uncertainty_without_creating_stop_authority(
        attempt, dead_submitter, monkeypatch):
    _close(attempt, 'blocked')
    calls = _control(monkeypatch, attempt)
    monkeypatch.setattr(dispatch, 'TERMINAL_WORKER_REAP_GRACE_SECONDS', 0)
    assert owner_stop_unresolved(attempt.board, attempt.task.id)
    assert owner_scope_stop_required(attempt.board, attempt.task.current_run_id) is False
    dispatch.reap_terminal_workers(attempt.board)
    assert _retained(attempt) is None
    assert calls == [] and _events(attempt) == []
    assert owner_stop_unresolved(attempt.board, attempt.task.id)
    assert kb.unblock_task(attempt.board, attempt.task.id) is False


def test_historical_stop_does_not_cover_a_later_native_admission(attempt, monkeypatch):
    _control(monkeypatch, attempt)
    assert kb.block_task(attempt.board, attempt.task.id, kind='needs_input')
    with kb.write_txn(attempt.board):
        kb._append_event(attempt.board, attempt.task.id, 'block_requested', {'reason': 'old Stop'})
    successor = _next_owner_run(attempt)
    _close(attempt, 'completed')
    assert owner_scope_stop_required(attempt.board, successor.current_run_id) is False
    assert not owner_stop_unresolved(attempt.board, attempt.task.id)
