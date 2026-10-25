"""Native cron admission reaches the public stateless delegation fallback without fake consent."""

import asyncio
import json
import threading
from unittest.mock import MagicMock

import pytest

from tests.gateway.test_native_cron_task_authority import _fire, _runtime, _settled


@pytest.mark.asyncio
async def test_persisted_cron_run_job_preserves_public_delegation_fallback(tmp_path, monkeypatch):
    from cron import scheduler
    from model_tools import handle_function_call
    from tests.tools.test_delegate_cron_sync_fallback import _make_real_child
    from tools import delegate_tool
    from tools.approval_context import current_approval_task_context

    _runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    child = _make_real_child()
    child_conversation = child.run_conversation
    observed, delegated = [], []
    effect = tmp_path / "delegated.txt"

    def child_model(*args, **kwargs):
        evidence = current_approval_task_context()
        result = child_conversation(*args, **kwargs)
        output = json.loads(handle_function_call("write_file", {"path": str(effect), "content": "delegated"},
                                                task_id="cron-delegated-child"))
        observed.append((evidence, output))
        return result

    child.run_conversation = child_model
    monkeypatch.setattr(delegate_tool, "_build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr(delegate_tool, "_resolve_delegation_credentials", lambda *_a, **_k: {
        "model": "fixture", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    })
    monkeypatch.setattr(scheduler, "_resolve_cron_agent_setup", lambda *_a: scheduler._CronAgentSetup(model="fixture"))

    def construct(_cls, _job, _cfg, _setup, *, workdir, session_id, session_db):
        parent = MagicMock()
        parent._delegate_depth = 0
        parent.session_id = session_id
        parent._interrupt_requested = False
        parent._active_children = []
        parent._active_children_lock = threading.Lock()
        parent._session_db = session_db

        def model(prompt, **_kwargs):
            from gateway.session_context import async_delivery_supported
            assert not async_delivery_supported()
            delegated.append(json.loads(delegate_tool.delegate_task(goal="Complete the scheduled child task",
                                                                    background=True, parent_agent=parent)))
            return {"final_response": "done", "messages": []}

        parent.run_conversation = model
        return parent

    monkeypatch.setattr(scheduler, "_construct_cron_agent", construct)
    try:
        _job, receipt = await _fire(authority)
        await _settled(authority, receipt)
        assert len(delegated) == 1
        assert delegated[0]["results"][0]["status"] == "completed", delegated
        assert delegated[0]["results"][0]["summary"] == "child work done", delegated
        evidence, result = observed[0]
        assert evidence.task_id == receipt["admission_id"]
        assert evidence.ancestry
        assert not result.get("error"), result
        assert effect.read_text() == "delegated"
    finally:
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")


@pytest.mark.asyncio
async def test_independent_cron_admission_binds_after_submitting_human_turn_completes(tmp_path, monkeypatch):
    from cron import scheduler
    from gateway import session_cron
    from gateway.session_contract import Principal, Submission
    from gateway.session_local import create_local_session
    from model_tools import handle_function_call

    runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    actor = Principal("human", authority.profile_id, frozenset({"session:create", "session:submit"}), "viewer")
    ref = create_local_session(authority, actor, dict(request_id="human", source="cli", model="fixture", toolsets=[]))
    waiting, release = asyncio.Event(), asyncio.Event()
    execute = session_cron.execute
    fires, observed = [], []
    effect = tmp_path / "independent.txt"

    async def held(*args):
        waiting.set()
        await release.wait()
        return await execute(*args)

    async def human_model(**_kwargs):
        _job, receipt = await _fire(authority)
        fires.append(receipt)
        return {"final_response": "scheduled", "messages": []}

    def cron_model(job, **_kwargs):
        observed.append(json.loads(handle_function_call("terminal",
            {"command": f"printf independent > {effect}", "background": True}, task_id="independent-cron")))
        return True, "document", "done", None

    monkeypatch.setattr(runner, "_run_agent", human_model)
    monkeypatch.setattr(session_cron, "execute", held)
    monkeypatch.setattr(scheduler, "run_job", cron_model)
    try:
        human = await authority.submit(actor, Submission("human-submit", ref, {"text": "Fire the stored job"}, "queue"))
        await asyncio.wait_for(waiting.wait(), 10)
        await _settled(authority, {"admission_id": human.admission_id})
        release.set()
        await _settled(authority, fires[0])
        assert not observed[0].get("error"), observed
        process = registry.get(observed[0]["session_id"])
        assert await asyncio.to_thread(process._completion_event.wait, 10)
        assert effect.read_text() == "independent"
    finally:
        release.set()
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")
