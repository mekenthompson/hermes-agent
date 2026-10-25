"""Native admission must establish authority before background work starts."""
import asyncio
import json
import socket
from types import SimpleNamespace

import pytest

from tests.gateway.test_plugin_origin_injection_authority import _context, _publish
from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["human", "plugin", "human_stop"])
async def test_background_admission_and_completion_authority(tmp_path, monkeypatch, origin):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal, Submission
    from gateway.session_local import create_local_session
    from hermes_state import AsyncSessionDB, SessionDB
    from hermes_state_runtime import list_session_admissions
    from tools import async_delegation as delegation
    from tools.approval import check_dangerous_command
    from tools.delegate_tool_dispatch import _dispatch_unit
    from tools.process_registry import process_registry

    def denied_connect(sock, address):
        raise AssertionError("external socket forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied_connect)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    executed = []

    def child(*args, **kwargs):
        executed.append(True)
        return {"results": [{"status": "completed", "summary": "fixture"}]}

    monkeypatch.setattr("tools.delegate_tool_dispatch._execute_and_aggregate", child)
    delegation._reset_for_tests()
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    db = SessionDB(tmp_path / "state.db")
    runner.session_store._db = db
    runner._session_db = AsyncSessionDB(db)
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="owner", db=db)
    viewer = Principal("test-owner", "default", frozenset({
        "session:create", "session:submit", "session:read", "session:control"}), "viewer")
    ref = create_local_session(authority, viewer, dict(
        request_id="cli", source="cli", model="fixture", toolsets=[]))
    await authority.attach(viewer, ref)
    live = authority.sessions[ref.session_id]
    target = runner.session_store.lookup_by_session_key(live.route).session_id
    ctx, manager = _context(tmp_path)
    _publish(runner, manager)
    dispatched, observed = [], []
    delegation_id = "deleg_authority_" + origin

    async def answer(*, message, **kwargs):
        if message == "work":
            unit = SimpleNamespace(
                children=[(0, {"goal": "fixture"}, SimpleNamespace())],
                task_list=[{"goal": "fixture"}], context=None, top_role="worker",
                creds={"model": "fixture"}, live_writers=[])
            routing = dict(session_key=live.route, origin_ui_session_id="", origin_session_id="",
                           parent_session_id=target, max_async_children=1)
            dispatched.append(await asyncio.to_thread(_dispatch_unit, unit, delegation_id, None, routing))
        else:
            observed.append(await asyncio.to_thread(check_dangerous_command, "printf fixture", "local"))
        return {"final_response": "ok", "messages": []}

    monkeypatch.setattr(runner, "_run_agent", answer)
    try:
        if origin == "plugin":
            assert await asyncio.to_thread(ctx.inject_message, "work", session_key=live.route)
        else:
            await authority.submit(viewer, Submission("work", ref, {"text": "work"}, "queue"))
        await async_wait_until(lambda: bool(dispatched), timeout=10, what="background admission")
        if origin == "plugin":
            assert dispatched[0]["status"] == "rejected"
            assert dispatched[0]["code"] == "missing_task_authority"
            assert not executed
            assert delegation.approval_reservation_for_completion(delegation_id) is None
            return
        assert dispatched[0]["status"] == "dispatched"
        await async_wait_until(lambda: all(row["status"] == "terminal" for row in list_session_admissions(
            db, session_id=ref.session_id, pending_only=False)), timeout=10, what="human admission settled")
        reservation = delegation.approval_reservation_for_completion(delegation_id)[1]
        assert reservation is not None
        if origin == "human_stop":
            await authority.interrupt(viewer, ref, authority._handle(ref).execution_generation)
            assert not reservation.lease.live()
        event = await asyncio.to_thread(process_registry.completion_queue.get, True, 10)
        assert await runner._deliver_completion_notification("completion", event)
        await async_wait_until(lambda: bool(observed), timeout=10, what="completion command guard")
        assert observed[0]["approved"] is (origin == "human")
    finally:
        delegation._reset_for_tests()
        db.close()


def test_authority_refusal_never_falls_back_to_inline_execution(monkeypatch):
    from tools import delegate_tool_dispatch as dispatch

    batch = SimpleNamespace(parent_agent=None, origin_wake_sid="wake", origin_session_history_delivery=True,
                            origin_ui_session_id="", live_deleg_id="deleg_refused")
    monkeypatch.setattr(dispatch, "_resolve_async_wake_sid", lambda *args: "wake")
    monkeypatch.setattr(dispatch, "_resolve_async_session_key", lambda *args: ("route", ""))
    from dataclasses import fields
    unit = dispatch._Batch(**{field.name: None for field in fields(dispatch._Batch)})
    unit.children = []
    monkeypatch.setattr(dispatch, "_units_of", lambda *args: [unit])
    monkeypatch.setattr(dispatch, "_dispatch_unit", lambda *args: {
        "status": "rejected", "code": "missing_task_authority", "error": "No origin authority"})
    monkeypatch.setattr(dispatch, "_run_sync_with_note", lambda *args: pytest.fail("unscoped fallback"))
    monkeypatch.setattr(dispatch, "_execute_and_aggregate", lambda *args, **kwargs: pytest.fail("inline work"))
    result = json.loads(dispatch._dispatch_background(batch))
    assert result["status"] == "rejected"
    assert result["code"] == "missing_task_authority"
