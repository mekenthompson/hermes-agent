"""Review commands inherit native identity and Stop without admitting synthetic messages."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.gateway.test_review_command import _make_agent
from tests.tools.test_delegate_apiserver_background import _patch_delegate
from tests.waiting import async_wait_until


@pytest.mark.asyncio
async def test_real_messaging_callback_authorizes_review(monkeypatch, tmp_path):
    from agent import review_engine
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tools import async_delegation as delegation

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")
    _patch_delegate(monkeypatch)
    monkeypatch.setattr(review_engine, "_load_review_credentials_cfg", lambda: None)
    delegation._reset_for_tests()
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fixture", typing_indicator=False))
    replies = []

    async def send(chat_id, content, reply_to=None, metadata=None):
        replies.append(content)
        return SendResult(success=True, message_id="sent")

    async def answer(**kwargs):
        return {"final_response": "ready", "messages": []}

    monkeypatch.setattr(adapter, "send", send)
    monkeypatch.setattr(runner, "_run_agent", answer)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="owner")
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id="42", chat_type="dm", user_id="42")
    try:
        await adapter.handle_message(MessageEvent(text="open a PR", source=source, message_id="request"))
        key = runner.session_store._generate_session_key(source)
        await async_wait_until(lambda: runner.session_store.lookup_by_session_key(key) is not None
                               and not adapter._background_tasks, timeout=15, what="ordinary native turn")
        entry = runner.session_store.lookup_by_session_key(key)
        parent = _make_agent()
        parent.session_id = entry.session_id
        runner._agent_cache[key] = parent
        await adapter.handle_message(MessageEvent(text="/review check tests", source=source, message_id="review"))
        await async_wait_until(lambda: any("Review started" in value for value in replies),
                               timeout=10, what="authenticated native review")
        live = authority.sessions[authority.logical_owner(entry.session_id)]
        assert list(live.approval_task_leases)
    finally:
        delegation._reset_for_tests()


@pytest.mark.asyncio
async def test_stop_during_command_hooks_refuses_late_review(monkeypatch, tmp_path):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_commands import execute_command
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from tools.approval_context import approval_task_revoked

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="owner")
    actor = Principal("owner", "default", frozenset({"session:create", "session:read", "session:control"}), "viewer")
    ref = create_local_session(authority, actor, dict(request_id="review", source="cli", model="fixture", toolsets=[]))
    await authority.attach(actor, ref)
    entered, release = asyncio.Event(), asyncio.Event()

    async def before(*args):
        entered.set()
        await release.wait()

    async def review(event):
        assert approval_task_revoked()
        from tools.registry import registry
        return registry.dispatch("kanban_create", {"title": "late", "assignee": "default"})

    monkeypatch.setattr("gateway.session_commands._pre_command_before", before)
    monkeypatch.setattr(runner, "_command_handler_table", lambda commands: {"review": review})
    pending = asyncio.create_task(execute_command(SimpleNamespace(authority=authority, actor=actor, subscriptions={}),
        ref, {"name": "review", "arg": ""}, dispatch=True))
    await asyncio.wait_for(entered.wait(), 10)
    await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
    release.set()
    reply = await asyncio.wait_for(pending, 10)
    assert "task_authority_revoked" in reply["output"]


@pytest.mark.asyncio
async def test_replaced_native_runtime_revokes_command_effects(monkeypatch, tmp_path):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_command_approval import command_task_scope
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from hermes_state_runtime import begin_runtime_epoch
    from tools.approval_context import current_approval_task_context
    from tools.registry import registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="owner")
    actor = Principal("owner", "default", frozenset({"session:create", "session:read", "session:control"}), "viewer")
    ref = create_local_session(authority, actor, dict(request_id="command", source="cli", model="fixture", toolsets=[]))
    await authority.attach(actor, ref)
    route = authority.sessions[ref.session_id].route
    with command_task_scope(authority, ref, actor.subject, "/review", authority.route_epoch(route)):
        assert current_approval_task_context() is not None
        begin_runtime_epoch(authority.db, instance_id="replacement")
        assert current_approval_task_context() is None
        effect = tmp_path / "stale-worker.txt"
        result = registry.dispatch("write_file", {"path": str(effect), "content": "stale"})
        assert "task_authority_revoked" in result
        assert not effect.exists()
