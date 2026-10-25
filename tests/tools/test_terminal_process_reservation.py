"""Process lifetime ownership is independent of completion notification policy."""
import json

import pytest

from tools.approval_context import approval_task_context, reserve_background_approval_task
from tools.process_registry import ProcessRegistry
from tools.terminal_tool_background import spawn_background_process


@pytest.fixture
def registry(monkeypatch):
    from tools import process_registry
    registry = ProcessRegistry()
    monkeypatch.setattr(process_registry, 'process_registry', registry)
    return registry


def _spawn(monkeypatch, registry, *, uncertain=False, fail=False):
    from tools import terminal_tool_background as background
    prepared = []
    def spawn(_registry, **params):
        session = registry._new_session('tracked descriptor', 'task', 'task', '', '.')
        params['prepare_session'](session)
        prepared.append(session)
        if fail:
            raise OSError('verified no-effect producer failure')
        registry._running[session.id] = session
        session.reconciliation_required = 'launch outcome unknown' if uncertain else ''
        return session
    monkeypatch.setattr(background, '_spawn', spawn)
    monkeypatch.setattr('tools.terminal_tool._resolve_command_cwd', lambda **kw: '.')
    with approval_task_context('Launch one owned process', identity='user', profile='owned', session='route',
                               task_id='original-admission', run_id='1', is_current=lambda: True):
        result = json.loads(spawn_background_process(command='tracked descriptor', env_type='local', env=None,
            cwd='.', workdir=None, effective_task_id='task', task_id='task', session_key='', effective_pty=False,
            notify_on_complete=False, watch_patterns=[], approval_note=None, pty_disabled_reason=None))
    return prepared[0], result


@pytest.mark.parametrize('uncertain', [False, True])
def test_silent_process_owns_reservation_until_actual_registry_finalizer(monkeypatch, registry, uncertain):
    from gateway.session_context import set_session_vars, clear_session_vars
    token = set_session_vars(async_delivery=False)
    try:
        session, result = _spawn(monkeypatch, registry, uncertain=uncertain)
    finally:
        clear_session_vars(token)
    reservation = session.approval_task_reservation
    assert reservation is not None and reservation.lease.live()
    assert reservation.evidence.task_id == 'original-admission'
    assert not session.notify_on_complete
    if uncertain:
        assert result['status'] == 'unknown'
    registry._finish_exited(session, 0)
    assert session.approval_task_reservation is None
    assert not reservation.active() and not reservation.lease.live()
    registry._move_to_finished(session)  # duplicate finalization cannot release the child twice
    assert not reservation.lease.live()


def test_verified_prelaunch_failure_releases_untracked_reservation(monkeypatch, registry):
    session, result = _spawn(monkeypatch, registry, fail=True)
    assert result['error']
    assert not session.approval_task_reservation.active()
    assert not session.approval_task_reservation.lease.live()
    assert not registry._running


def test_notification_promotion_replaces_and_releases_prior_process_reservation(registry):
    from tools.process_registry_wait_policy import _claim_notify_if_running
    session = registry._new_session('pending', 'task', 'task', 'route', '.')
    registry._running[session.id] = session
    with approval_task_context('Original process', identity='user', profile='owned', session='route',
                               task_id='first', is_current=lambda: True):
        previous = session.approval_task_reservation = reserve_background_approval_task()
    assert previous.lease.live()
    with approval_task_context('Follow the process', identity='user', profile='owned', session='route',
                               task_id='second', is_current=lambda: True):
        assert _claim_notify_if_running(registry, session)
    current = session.approval_task_reservation
    try:
        assert current is not previous and current.evidence.task_id == 'second'
        assert current.lease.live()
        assert not previous.active() and not previous.lease.live()
    finally:
        current.release()
        registry._running.pop(session.id)
