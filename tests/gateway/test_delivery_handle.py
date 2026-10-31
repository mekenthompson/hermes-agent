"""A delivery snapshot must not adopt, attach or refresh a retained conversation."""
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_desktop_delivery_handle_requires_explicit_transition_and_is_read_only(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'gui': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    db = store._db
    sid = '20261030_184546_abcdef'
    db.create_session(sid, source='desktop', model='retained-model', cwd=str(tmp_path))
    db.append_message(sid, role='user', content='retained history')
    db.end_session(sid, 'session_switch')
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id='first')
    owner = AuthorityConnection(authority, object(), {'provider': 'local', 'user_id': 'native',
        'profile_id': str(tmp_path), 'instance_id': 'first'})
    request = {'id': 1, 'method': 'session.delivery_handle', 'params': {'session_id': sid}}
    before = db.get_session(sid)
    refused = await owner.dispatch(request)
    assert refused.get('error', {}).get('message') == 'not_found', refused
    assert db.get_session(sid) == before
    resumed = await owner.dispatch({'id': 2, 'method': 'session.resume', 'params': {'session_id': sid}})
    assert resumed.get('result', {}).get('session_id') == sid, resumed
    before = db.get_session(sid)
    subscriptions = dict(owner.subscriptions)
    result = await owner.dispatch(request)
    assert result.get('result', {}).get('session_id') == sid, result
    assert result['result']['stored_session_id'] == sid
    assert result['result']['profile_id'] == str(tmp_path)
    assert result['result']['revision'] == before['runtime_revision']
    assert result['result']['execution_generation'] == before['runtime_generation']
    assert db.get_session(sid) == before
    assert owner.subscriptions == subscriptions
    foreign = AuthorityConnection(authority, object(), {'provider': 'local', 'user_id': 'other',
        'profile_id': str(tmp_path), 'instance_id': 'first'})
    denied = await foreign.dispatch(request)
    assert denied.get('error', {}).get('message') == 'permission_denied', denied
    unknown = await owner.dispatch({**request, 'params': {'session_id': 'missing'}})
    assert unknown.get('error', {}).get('message') == 'not_found', unknown
    cross_profile = await owner.dispatch({**request, 'params': {'session_id': sid, 'profile': 'foreign'}})
    assert cross_profile.get('error', {}).get('message') == 'profile_mismatch', cross_profile
    await foreign.close()
    from gateway.run import GatewayRunner
    from gateway.session_context import get_session_env
    from gateway.session import SessionContext
    live = authority.sessions[sid]
    context = SessionContext(source=live.source, session_key=live.route, session_id=sid,
                             connected_platforms=[], home_channels={})
    tokens = GatewayRunner._set_session_env(runner, context)
    try:
        assert get_session_env('HERMES_SESSION_SOURCE') == 'desktop'
        assert get_session_env('HERMES_SESSION_CHAT_ID') == sid
        assert get_session_env('HERMES_SESSION_ID') == sid
        assert get_session_env('HERMES_UI_SESSION_ID') == ''
    finally:
        GatewayRunner._clear_session_env(runner, tokens)
    notice = {'session_id': sid, 'request_id': 'notice-test', 'expected_revision': before['runtime_revision'],
              'expected_generation': before['runtime_generation'], 'operation': 'notice', 'payload': {'text': 'progress'}}
    delivered = await owner.dispatch({'id': 3, 'method': 'session.mutate', 'params': notice})
    assert delivered.get('result', {}).get('notice_message_uid'), delivered
    import hermes_state_runtime as rt
    rt.admit_session_input(db, epoch=authority.epoch, principal_id=owner.actor.subject,
                           session_id=sid, request_id='human-turn', payload={'text': 'pending'})
    rt.claim_session_input(db, epoch=authority.epoch, session_id=sid)
    generation = db.get_session(sid)['runtime_generation']
    assert generation > before['runtime_generation']
    stopped = await owner.dispatch({'id': 4, 'method': 'session.interrupt',
                                   'params': {'session_id': sid, 'execution_generation': generation}})
    assert 'result' in stopped, stopped
    stale = await owner.dispatch({'id': 5, 'method': 'session.mutate',
                                 'params': {**notice, 'request_id': 'after-stop',
                                            'expected_revision': db.get_session(sid)['runtime_revision']}})
    assert stale.get('error', {}).get('message') == 'stale_generation', stale
    await owner.close()
    # A read cannot perform cold restore/compression recovery after eviction.
    authority.sessions.clear()
    before = db.get_session(sid)
    refused = await owner.dispatch(request)
    assert refused.get('error', {}).get('message') == 'not_found', refused
    assert db.get_session(sid) == before
    assert authority.sessions == {}
