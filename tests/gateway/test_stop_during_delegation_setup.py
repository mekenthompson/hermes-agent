"""Native Stop must fence delegation setup and inline fallback after asynchronous work."""
import asyncio
import json
import threading

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["routes", "first_child", "fallback"])
async def test_native_stop_during_producer_prevents_future_setup(tmp_path, monkeypatch, stage):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from gateway.session_command_approval import command_task_scope
    from gateway.session_context import set_session_vars
    from tests.tools.test_delegate_apiserver_background import _patch_delegate, _fake_parent

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    dt = _patch_delegate(monkeypatch)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    actor = Principal('owner', 'default', frozenset({'session:create', 'session:read', 'session:control'}), 'viewer')
    ref = create_local_session(authority, actor, dict(request_id='review', source='cli', model='fixture', toolsets=[]))
    await authority.attach(actor, ref)
    route = authority.sessions[ref.session_id].route
    set_session_vars(platform='telegram', chat_id='chat', session_key=route, session_id=ref.session_id, async_delivery=True)
    entered, release = threading.Event(), threading.Event()
    original_routes, original_build = dt._routes_for, dt._build_child_agent
    built = []

    def delayed_routes(*args, **kwargs):
        routes = original_routes(*args, **kwargs)
        if stage == "routes":
            entered.set()
            assert release.wait(10)
        return routes

    def build(**kwargs):
        built.append(True)
        child = original_build(**kwargs)
        if stage == "first_child":
            entered.set()
            assert release.wait(10)
        return child

    if stage == "fallback":
        def delayed_target(*args):
            entered.set()
            assert release.wait(10)
        monkeypatch.setattr("tools.delegate_tool_dispatch._resolve_async_wake_sid", delayed_target)
        monkeypatch.setattr(dt, "_run_single_child", lambda *a, **kw: pytest.fail("effectful inline fallback"))
    monkeypatch.setattr(dt, '_routes_for', delayed_routes)
    monkeypatch.setattr(dt, '_build_child_agent', build)
    monkeypatch.setattr(dt, '_parent_live_home', lambda parent: tmp_path)
    with command_task_scope(authority, ref, actor.subject, '/review tests', authority.route_epoch(route)):
        pending = asyncio.create_task(asyncio.to_thread(dt.delegate_task,
            tasks=[{"goal": "Review the first part"}, {"goal": "Review the second part"}] if stage == "first_child" else [{"goal": "review tests"}],
            background=True, parent_agent=_fake_parent()))
        if not await asyncio.to_thread(entered.wait, 10):
            assert not pending.done(), pending.result() if pending.done() else "producer did not reach stage"
            pytest.fail("producer did not reach stage")
        await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
        release.set()
        reply = json.loads(await asyncio.wait_for(pending, 10))
    assert reply['code'] == 'task_authority_revoked', reply
    transcripts = list(tmp_path.rglob('task-*.log'))
    if stage == "routes":
        assert not built and not transcripts, {"built": len(built), "transcripts": transcripts, "reply": reply}
    else:
        assert len(built) == 1, "only setup begun before Stop may finish; no new sibling may start"
