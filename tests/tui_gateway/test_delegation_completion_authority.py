"""Authenticated Desktop delegation must retain authority into its completion turn."""
import json
import threading
from queue import Empty
from types import SimpleNamespace

import pytest

from tests.tui_gateway.test_failed_turn_retention import turn_env
from tests.tui_gateway.test_approval_request_origin import LoginSocket
from tui_gateway import server

_REAL_THREAD = threading.Thread


class _LoginSocket:
    auth_identity = LoginSocket.auth_identity

    def write(self, obj: dict) -> bool:
        return True

    def close(self) -> None:
        pass


@pytest.mark.parametrize('human_identity', [True, False], ids=['human-login', 'dashboard-token'])
@pytest.mark.parametrize('through_executor', [False, True], ids=['producer', 'tool-executor'])
@pytest.mark.parametrize('stopped', [False, True], ids=['normal-completion', 'human-stop'])
@pytest.mark.usefixtures('turn_env')
def test_authenticated_desktop_delegation_completion_authority(
    tmp_path, monkeypatch, stopped, through_executor, human_identity,
):
    from hermes_state import SessionDB
    from tools import approval_context, async_delegation as delegation
    from tools.delegate_tool_dispatch import _dispatch_unit, _resolve_async_session_key
    from tools.process_registry import process_registry
    from tools.process_registry_notifications import format_process_notification
    from tools.terminal_tool import terminal_tool
    from tui_gateway.transport import bind_transport, reset_transport

    delegation._reset_for_tests()
    while True:
        try:
            process_registry.completion_queue.get_nowait()
        except Empty:
            break
    db = SessionDB(tmp_path / 'state.db')
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(server, '_schedule_agent_build', lambda _sid: None)
    monkeypatch.setattr(server, '_schedule_session_cap_enforcement', lambda: None)
    monkeypatch.setattr(server, '_start_agent_build', lambda *_a: None)
    monkeypatch.setattr(server, '_restart_completed_failed_agent_build', lambda *_a: False)
    monkeypatch.setattr(server, '_wait_agent_for_prompt', lambda *_a: None)
    monkeypatch.setattr(server, '_start_session_services', lambda *_a: None)
    monkeypatch.setattr(approval_context, '_get_approval_mode', lambda: 'off')
    release = threading.Event()
    started = threading.Event()
    decisions = []
    handles = []

    def model_runner(*_args, **_kwargs):
        started.set()
        assert release.wait(10), 'fixture did not release the delegation'
        return {'results': [{'task_index': 0, 'status': 'completed', 'summary': 'done'}]}

    monkeypatch.setattr('tools.delegate_tool_dispatch._execute_and_aggregate', model_runner)

    def dispatch_delegation(_args=None):
        unit = SimpleNamespace(children=[(0, {'goal': 'work'}, SimpleNamespace())],
                               task_list=[{'goal': 'work'}], context=None, top_role='leaf',
                               creds={'model': 'fixture'}, live_writers=[])
        route, origin = _resolve_async_session_key(session['agent'], sid)
        routing = dict(session_key=route, origin_ui_session_id=origin,
                       origin_session_id='', parent_session_id=session['agent'].session_id,
                       max_async_children=1)
        handle = _dispatch_unit(unit, 'deleg_desktop_authority', None, routing)
        handles.append(handle)
        return json.dumps(handle)

    def run(*_args, **_kwargs):
        inline_thread = server.threading.Thread
        server.threading.Thread = _REAL_THREAD
        try:
            if not handles:
                direct_evidence = approval_context.current_approval_task_context()
                assert (direct_evidence is not None) == human_identity
                direct = json.loads(terminal_tool('printf direct', task_id=session['session_key']))
                assert direct.get('output', '').strip() == 'direct', direct
                if through_executor:
                    from tests.agent.test_tool_call_guardrail_runtime import _mock_tool_call
                    call = _mock_tool_call('delegate_task', '{"tasks":[{"goal":"work"}]}')
                    session['agent']._execute_tool_calls_sequential(
                        SimpleNamespace(content='', tool_calls=[call]), [], session['session_key'])
                else:
                    dispatch_delegation()
            else:
                evidence = approval_context.current_approval_task_context()
                result = json.loads(terminal_tool('printf ok', task_id=session['session_key']))
                decisions.append((evidence, result))
        finally:
            server.threading.Thread = inline_thread
        return {'final_response': 'done', 'messages': []}

    transport = _LoginSocket()
    if not human_identity:
        transport.auth_identity = {'provider': 'dashboard-session-token', 'user_id': 'dashboard-session-token'}
    token = bind_transport(transport)
    try:
        created = server.handle_request({'id': 'create', 'method': 'session.create',
                                         'params': {'source': 'desktop', 'cwd': str(tmp_path)}})
        assert 'result' in created, created
        sid = created['result']['session_id']
        session = server._sessions[sid]
        if through_executor:
            from tests.agent.test_tool_call_guardrail_runtime import _make_agent
            inline_thread = server.threading.Thread
            server.threading.Thread = _REAL_THREAD
            try:
                session['agent'] = _make_agent('delegate_task', platform='desktop')
            finally:
                server.threading.Thread = inline_thread
            session['agent'].session_id = session['session_key']
            monkeypatch.setattr(session['agent'], 'run_conversation', run)
            monkeypatch.setattr(session['agent'], '_dispatch_delegate_task', dispatch_delegation)
        else:
            session['agent'] = SimpleNamespace(session_id=session['session_key'],
                                               run_conversation=run, clear_interrupt=lambda: None)
        submitted = server.handle_request({'id': 'prompt', 'method': 'prompt.submit',
                                           'params': {'session_id': sid, 'text': 'Inspect the build'}})
        assert submitted['result']['status'] == 'streaming', submitted
        if not human_identity:
            assert len(handles) == 1 and handles[0]["code"] == "missing_task_authority", handles
            assert not started.is_set(), "Unscoped transport must refuse before child execution"
            assert process_registry.completion_queue.empty()
            assert not delegation._records
            return
        assert handles == [{'status': 'dispatched', 'delegation_id': 'deleg_desktop_authority'}]
        assert started.wait(10)
        with delegation._records_lock:
            reservation = delegation._records['deleg_desktop_authority']['_approval_reservation']
        if human_identity:
            assert reservation is not None and reservation.active() and reservation.lease.live()
            assert not reservation.lease.parent_open()
        else:
            assert reservation is None
        if stopped:
            stopped_result = server.handle_request({'id': 'stop', 'method': 'session.interrupt',
                                                    'params': {'session_id': sid}})
            assert stopped_result['result']['status'] == 'interrupted', stopped_result
            if reservation is not None:
                assert not reservation.lease.live()
                assert reservation.active(), 'a running producer must retain its reservation'
        release.set()
        event = process_registry.completion_queue.get(timeout=10)
        assert event['type'] == 'async_delegation'
        assert event['delegation_id'] == 'deleg_desktop_authority'
        server._notif_handle_ready(sid, session, [event], set(), process_registry,
                                   format_process_notification, None)
        if stopped:
            assert not decisions, 'Stop must not admit the completion turn'
            assert process_registry.completion_queue.get_nowait() == event
        elif human_identity:
            assert len(decisions) == 1, decisions
            evidence, result = decisions[0]
            assert result.get('output', '').strip() == 'ok', (evidence, result)
            assert evidence.text == 'Inspect the build'
            assert evidence.ancestry[-1].startswith('completion:')
        else:
            assert len(decisions) == 1, decisions
            evidence, result = decisions[0]
            assert evidence is None
            assert result.get('status') == 'blocked'
            assert result.get('error') == 'The originating task was stopped or completed.'
        if reservation is not None:
            assert not reservation.active()
    finally:
        release.set()
        reset_transport(token)
        delegation._reset_for_tests()
        if 'sid' in locals():
            server._sessions.pop(sid, None)
        db.close()
