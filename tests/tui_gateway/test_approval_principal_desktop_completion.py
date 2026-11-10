"""A Desktop process completion keeps its submit's command authority for every real socket identity.

Regression for HF-683: the dashboard and native sockets stamp extra keys (``profile_scope``,
``issuer``...) onto the server identity, so the bare ``{user_id, provider}`` owner projection was
None for every real Desktop socket. The submit got no approval lease, its background process held
no reservation, and the completion turn refused every approval-gated command as "stopped or
completed". The fix admits the submit through a separate approval principal; session ownership
(``transport_owner``), attach gating and browser control keep their bare-identity semantics.
"""
import json
import threading
import time
from queue import Empty
from types import SimpleNamespace

import pytest

from tests.tui_gateway.test_failed_turn_retention import turn_env
from tui_gateway import server

_REAL_THREAD = threading.Thread

BARE = {'provider': 'basic', 'user_id': 'alice'}
DASHBOARD_BASIC = {'provider': 'basic', 'user_id': 'alice', 'profile_scope': 'host'}
DASHBOARD_NOUS = {'provider': 'nous', 'user_id': 'nas_user:alice', 'profile_scope': 'host'}
OAUTH_TICKET_WITH_ISSUER = {'provider': 'nous', 'user_id': 'nas_user:alice', 'issuer': 'https://portal.example'}
SESSION_TOKEN = {'provider': 'dashboard-session-token', 'user_id': 'dashboard-session-token',
                 'profile_scope': 'host'}
OTHER_LOGIN = {'provider': 'nous', 'user_id': 'nas_user:mallory', 'profile_scope': 'host'}


class _Socket:
    def __init__(self, identity):
        self.auth_identity = identity

    def write(self, _frame):
        return True

    def close(self):
        pass


def _drain(queue):
    while True:
        try:
            queue.get_nowait()
        except Empty:
            return


def _run_completion(tmp_path, monkeypatch, *, creator, submitter):
    """Create over ``creator`` (None = stdio), submit + spawn over ``submitter``, deliver the completion,
    and return (session, completion-turn terminal verdict)."""
    from hermes_state import SessionDB
    from tools import approval_context
    from tools.process_registry import ProcessSession, process_registry
    from tools.process_registry_notifications import format_process_notification
    from tools.terminal_tool import terminal_tool
    from tools.terminal_tool_background import spawn_background_process
    from tui_gateway.transport import bind_transport, reset_transport

    _drain(process_registry.completion_queue)
    db = SessionDB(tmp_path / 'state.db')
    for attr, value in (('_get_db', lambda: db), ('_schedule_agent_build', lambda _sid: None),
                        ('_schedule_session_cap_enforcement', lambda: None),
                        ('_start_agent_build', lambda *_a: None),
                        ('_restart_completed_failed_agent_build', lambda *_a: False),
                        ('_wait_agent_for_prompt', lambda *_a: None),
                        ('_start_session_services', lambda *_a: None)):
        monkeypatch.setattr(server, attr, value)
    monkeypatch.setattr(approval_context, '_get_approval_mode', lambda: 'off')
    proc = ProcessSession(id=f'proc_hf683_{time.monotonic_ns()}', command='printf done', started_at=time.time())

    def spawn(_registry, **kwargs):
        proc.session_key = kwargs['session_key']
        with process_registry._lock:
            process_registry._running[proc.id] = proc
        return proc

    monkeypatch.setattr('tools.terminal_tool_background._spawn', spawn)
    verdicts, spawned = [], []

    def run(*_args, **_kwargs):
        if not spawned:
            spawned.append(True)
            spawn_background_process(
                command='printf done', env=SimpleNamespace(env={}), env_type='local',
                effective_task_id='fixture-task', task_id='fixture-task',
                session_key=session['session_key'], workdir=None, cwd=str(tmp_path),
                effective_pty=False, notify_on_complete=True, watch_patterns=None,
                approval_note=None, pty_disabled_reason=None)
        else:
            inline = server.threading.Thread
            server.threading.Thread = _REAL_THREAD
            try:
                verdicts.append(json.loads(terminal_tool('printf ok', task_id=session['session_key'])))
            finally:
                server.threading.Thread = inline
        return {'final_response': 'done', 'messages': []}

    # One socket object throughout: a second attached socket turns the slot into a FanoutTransport,
    # whose writer the inline-thread fixture would spin forever. A login change on the same socket
    # still exercises the server-side principal comparison at submit.
    socket = _Socket(creator)
    token = bind_transport(socket) if creator is not None else None
    sid = None
    try:
        created = server.handle_request({'id': 'c', 'method': 'session.create',
                                         'params': {'source': 'desktop', 'cwd': str(tmp_path)}})
        sid = created['result']['session_id']
        session = server._sessions[sid]
        session['agent'] = SimpleNamespace(session_id=session['session_key'], run_conversation=run,
                                           clear_interrupt=lambda: None)
        socket.auth_identity = submitter
        if token is None:
            token = bind_transport(socket)
        submitted = server.handle_request({'id': 'p', 'method': 'prompt.submit',
                                           'params': {'session_id': sid, 'text': 'Run the build'}})
        assert submitted.get('result', {}).get('status') == 'streaming', submitted
        assert spawned, 'the submit turn did not run'
        process_registry._finish_exited(proc, 0)
        event = process_registry.completion_queue.get(timeout=5)
        server._notif_handle_ready(sid, session, [event], set(), process_registry,
                                   format_process_notification, None)
        assert len(verdicts) == 1, verdicts
        return session, verdicts[0]
    finally:
        if token is not None:
            reset_transport(token)
        with process_registry._lock:
            process_registry._running.pop(proc.id, None)
            process_registry._finished.pop(proc.id, None)
        if sid is not None:
            server._sessions.pop(sid, None)
        db.close()


@pytest.mark.parametrize('identity', [BARE, DASHBOARD_BASIC, DASHBOARD_NOUS, OAUTH_TICKET_WITH_ISSUER, SESSION_TOKEN],
                         ids=['bare-ticket', 'dashboard-basic', 'dashboard-nous', 'oauth-issuer', 'session-token'])
@pytest.mark.usefixtures('turn_env')
def test_completion_turn_keeps_command_authority(tmp_path, monkeypatch, identity):
    from tui_gateway.session_transports import _server_verified_transport_owner
    session, verdict = _run_completion(tmp_path, monkeypatch, creator=identity, submitter=identity)
    assert verdict.get('output', '').strip() == 'ok', verdict
    assert session['approval_principal'] and session['approval_principal'].startswith('principal:dashboard:')
    # Ownership semantics are untouched: only the bare ticket identity projects to an owner.
    assert session['transport_owner'] == _server_verified_transport_owner(_Socket(identity))


@pytest.mark.usefixtures('turn_env')
def test_foreign_login_submit_gets_no_completion_authority(tmp_path, monkeypatch):
    from tools.approval_context import UNVERIFIED_ORIGIN_DENIAL
    _session, verdict = _run_completion(tmp_path, monkeypatch, creator=DASHBOARD_NOUS, submitter=OTHER_LOGIN)
    assert verdict.get('status') == 'blocked', verdict
    assert verdict.get('error') == UNVERIFIED_ORIGIN_DENIAL


@pytest.mark.usefixtures('turn_env')
def test_stdio_created_session_desktop_submit_gets_no_completion_authority(tmp_path, monkeypatch):
    from tools.approval_context import UNVERIFIED_ORIGIN_DENIAL
    session, verdict = _run_completion(tmp_path, monkeypatch, creator=None, submitter=SESSION_TOKEN)
    assert session['approval_principal'] is None
    assert verdict.get('status') == 'blocked', verdict
    assert verdict.get('error') == UNVERIFIED_ORIGIN_DENIAL


def test_internal_and_missing_identities_have_no_principal():
    from hermes_cli.dashboard_auth.ws_tickets import INTERNAL_PROVIDER, INTERNAL_USER_ID
    from tui_gateway.session_transports import _transport_approval_principal
    assert _transport_approval_principal(_Socket(None)) is None
    assert _transport_approval_principal(_Socket({'provider': INTERNAL_PROVIDER, 'user_id': INTERNAL_USER_ID})) is None
    assert _transport_approval_principal(_Socket({'provider': 'nous'})) is None
    # Distinct logins and distinct issuers never share a principal.
    assert _transport_approval_principal(_Socket(DASHBOARD_NOUS)) != _transport_approval_principal(_Socket(OTHER_LOGIN))
    assert (_transport_approval_principal(_Socket(OAUTH_TICKET_WITH_ISSUER))
            != _transport_approval_principal(_Socket({**OAUTH_TICKET_WITH_ISSUER, 'issuer': 'https://other.example'})))
