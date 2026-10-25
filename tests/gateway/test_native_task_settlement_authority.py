"""Reserved native children retain authority while a successful parent awaits its terminal commit."""

import asyncio
import json
import threading
from unittest.mock import MagicMock

import pytest

from tests.gateway.test_native_cron_task_authority import _fire, _runtime, _settled
from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "interrupted", "stop", "generation", "replacement"])
async def test_human_delegated_terminal_while_parent_settlement_is_pending(tmp_path, monkeypatch, outcome):
    from gateway import session_settlement_recovery
    from gateway.session_contract import Principal, Submission
    from gateway.session_local import create_local_session
    from hermes_state_runtime import get_session_admission
    from model_tools import handle_function_call
    from tests.tools.test_delegate_cron_sync_fallback import _make_real_child
    from tools import delegate_tool

    runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    actor = Principal("human", authority.profile_id, frozenset({"session:create", "session:submit", "session:read", "session:control"}), "viewer")
    ref = create_local_session(authority, actor, dict(request_id="human", source="cli", model="fixture", toolsets=[]))
    await authority.attach(actor, ref)
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = ref.session_id
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._session_db = authority.db
    child = _make_real_child()
    child_ready, run_child = threading.Event(), threading.Event()
    settling, release_settlement = asyncio.Event(), asyncio.Event()
    observed, dispatched = [], []
    effect = tmp_path / "settling-child.txt"

    def child_model(*_args, **_kwargs):
        child_ready.set()
        assert run_child.wait(15)
        result = json.loads(handle_function_call("terminal",
            {"command": f"printf child > {effect}", "background": True}, task_id="settling-child"))
        if result.get("session_id"):
            assert registry.get(result["session_id"])._completion_event.wait(10)
        observed.append(result)
        return {"final_response": "child done", "messages": []}

    child.run_conversation = child_model
    monkeypatch.setattr(delegate_tool, "_build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr(delegate_tool, "_resolve_delegation_credentials", lambda *_a, **_k: {
        "model": "fixture", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    })

    async def human_model(**_kwargs):
        dispatched.append(json.loads(await asyncio.to_thread(delegate_tool.delegate_task,
            goal="Start the child process", background=True, parent_agent=parent)))
        assert await asyncio.to_thread(child_ready.wait, 10)
        result = {"final_response": "parent done", "messages": [],
                  **({outcome: True, "completed": False} if outcome in ("failed", "interrupted") else {})}
        if outcome in ("failed", "interrupted"):
            from gateway.session_results import record_terminal_failure
            record_terminal_failure(result)
        return result

    settle = session_settlement_recovery.settle_with_retry

    async def held(*args, **kwargs):
        settling.set()
        await release_settlement.wait()
        return await settle(*args, **kwargs)

    monkeypatch.setattr(runner, "_run_agent", human_model)
    monkeypatch.setattr(session_settlement_recovery, "settle_with_retry", held)
    try:
        receipt = await authority.submit(actor, Submission("human-request", ref, {"text": "Start the authorized child"}, "queue"))
        await asyncio.wait_for(settling.wait(), 15)
        assert dispatched[0]["status"] == "dispatched", dispatched
        row = get_session_admission(authority.db, admission_id=receipt.admission_id)
        assert row["status"] == "started"
        assert ref.session_id not in authority.executing
        if outcome in ("failed", "interrupted"):
            assert authority.pending_results[receipt.admission_id]["result"][outcome] is True
        if outcome == "stop":
            await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
        elif outcome == "generation":
            runner._begin_session_run_generation(authority.sessions[ref.session_id].route)
        elif outcome == "replacement":
            from gateway.config import GatewayConfig
            from gateway.run import GatewayRunner
            from gateway.session_authority import initialize_session_authority
            successor = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "successor-sessions"))
            successor.session_store._db = successor._session_db = authority.db
            await initialize_session_authority(successor, profile_id=authority.profile_id, instance_id="successor", db=authority.db)
        run_child.set()
        await async_wait_until(lambda: bool(observed), timeout=15, what="delegated terminal in settlement window")
        if outcome == "completed":
            assert not observed[0].get("error"), observed
            assert effect.read_text() == "child"
        else:
            assert observed[0].get("code") == "task_authority_revoked", observed
            assert not effect.exists()
        release_settlement.set()
        if outcome != "replacement":
            await _settled(authority, {"admission_id": receipt.admission_id})
    finally:
        run_child.set()
        release_settlement.set()
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancel"])
async def test_cron_reserved_child_while_parent_settlement_is_pending(tmp_path, monkeypatch, outcome):
    from cron import scheduler
    from gateway import session_cron, session_settlement_recovery
    from hermes_state_runtime import get_session_admission
    from model_tools import handle_function_call
    from tools.approval_context import background_approval_task, reserve_background_approval_task

    _runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    settling, release = asyncio.Event(), asyncio.Event()
    reservations = []
    effect = tmp_path / "cron-settling-child.txt"

    def cron_model(job, **_kwargs):
        reservations.append(reserve_background_approval_task())
        ok = outcome != "failed"
        return ok, "document", "done", None if ok else "model failed"

    settle = session_settlement_recovery.settle_with_retry

    async def held(*args, **kwargs):
        settling.set()
        await release.wait()
        return await settle(*args, **kwargs)

    monkeypatch.setattr(scheduler, "run_job", cron_model)
    monkeypatch.setattr(session_settlement_recovery, "settle_with_retry", held)
    try:
        _job, receipt = await _fire(authority)
        await asyncio.wait_for(settling.wait(), 15)
        assert reservations[0] is not None
        row = get_session_admission(authority.db, admission_id=receipt["admission_id"])
        assert row["status"] == "started"
        assert receipt["session_id"] not in authority.executing
        if outcome == "cancel":
            await session_cron.operation(authority, "cancel", receipt)

        def child():
            with background_approval_task(reservations[0], "cron-settling-child"):
                return json.loads(handle_function_call("write_file", {"path": str(effect), "content": "scheduled"},
                                                       task_id="cron-settling-child"))

        result = await asyncio.to_thread(child)
        if outcome == "completed":
            assert effect.read_text() == "scheduled", result
        else:
            assert result.get("code") == "task_authority_revoked", result
            assert not effect.exists()
        release.set()
        await _settled(authority, receipt)
    finally:
        release.set()
        for reservation in reservations:
            if reservation is not None:
                reservation.release()
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")
