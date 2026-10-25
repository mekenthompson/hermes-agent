"""Persisted cron fires and detached native children keep only their live owner authority."""

import asyncio
import json
import threading

import pytest

from tests.waiting import async_wait_until


async def _runtime(tmp_path, monkeypatch, *, paused=False):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from tools import process_registry, terminal_tool

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setattr(run, "_load_gateway_config", lambda: {"model": {}, "platform_toolsets": {"cli": []}})
    runner = GatewayRunner(GatewayConfig(sessions_dir=home / "sessions"))
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id="owner")
    if paused:
        monkeypatch.setattr(authority, "_schedule", lambda ref: None)
    config = terminal_tool._get_env_config
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {**config(), "env_type": "local", "cwd": str(tmp_path)})
    registry = process_registry.ProcessRegistry()
    monkeypatch.setattr(process_registry, "process_registry", registry)
    return runner, authority, registry


async def _fire(authority):
    from cron.jobs import create_job
    from gateway.session_cron import operation

    job = create_job("Perform the stored scheduled task", "0 * * * *", model="fixture")
    receipt = await operation(authority, "submit", {"job_id": job["id"], "request_id": "native-fire", "extra_prompt": None})
    return job, receipt


async def _settled(authority, receipt):
    from hermes_state_runtime import get_session_admission

    await async_wait_until(lambda: get_session_admission(authority.db, admission_id=receipt["admission_id"])["status"] == "terminal",
                          timeout=15, what="native admission settlement")


@pytest.mark.asyncio
async def test_persisted_cron_admission_starts_real_background_terminal(tmp_path, monkeypatch):
    from cron import scheduler
    from model_tools import handle_function_call
    from tools.approval_context import current_approval_task_context
    from hermes_state_keys import CRON_PRINCIPAL_ID

    _runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    effect = tmp_path / "scheduled.txt"
    observed = []

    def model_execution(job, **_kwargs):
        evidence = current_approval_task_context()
        result = json.loads(handle_function_call("terminal", {"command": f"printf scheduled > {effect}", "background": True},
                                                 task_id="cron-native"))
        observed.append((evidence, result))
        return True, "document", "done", None

    monkeypatch.setattr(scheduler, "run_job", model_execution)
    try:
        _job, receipt = await _fire(authority)
        await _settled(authority, receipt)
        evidence, result = observed[0]
        assert not result.get("error"), result
        assert evidence.identity == CRON_PRINCIPAL_ID
        assert evidence.profile == authority.profile_id
        assert evidence.task_id == receipt["admission_id"]
        assert evidence.text == "Perform the stored scheduled task"
        process = registry.get(result["session_id"])
        assert await asyncio.to_thread(process._completion_event.wait, 10)
        assert effect.read_text() == "scheduled"
    finally:
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["complete", "cancel", "cancel_completed", "stop", "replace"])
async def test_cron_reserved_child_after_parent_settlement_or_revocation(tmp_path, monkeypatch, finish):
    from cron import scheduler
    from gateway.session_contract import Principal, SessionRef
    from gateway.session_cron import operation
    from gateway.session_authority import initialize_session_authority
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from model_tools import handle_function_call
    from tools.approval_context import background_approval_task, reserve_background_approval_task

    _runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    ready, release = threading.Event(), threading.Event()
    reservation, effects = [], []
    effect = tmp_path / "child.txt"

    def model_execution(job, **_kwargs):
        reservation.append(reserve_background_approval_task())
        ready.set()
        assert release.wait(15)
        return True, "document", "done", None

    monkeypatch.setattr(scheduler, "run_job", model_execution)
    try:
        _job, receipt = await _fire(authority)
        assert await asyncio.to_thread(ready.wait, 10)
        assert reservation[0] is not None
        ref = SessionRef(authority.profile_id, receipt["session_id"])
        if finish == "cancel":
            await operation(authority, "cancel", receipt)
        elif finish == "stop":
            actor = Principal("controller", authority.profile_id, frozenset({"session:control", "session:operator"}), "viewer")
            await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
        release.set()
        await _settled(authority, receipt)
        if finish == "cancel_completed":
            assert reservation[0].lease.live()
            await operation(authority, "cancel", receipt)
        if finish == "replace":
            successor = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "successor-sessions"))
            successor.session_store._db = successor._session_db = authority.db
            await initialize_session_authority(successor, profile_id=authority.profile_id, instance_id="successor", db=authority.db)

        def child_effect():
            with background_approval_task(reservation[0], "cron-child"):
                effects.append(json.loads(handle_function_call("write_file", {"path": str(effect), "content": "scheduled child"},
                                                               task_id="cron-child")))

        await asyncio.to_thread(child_effect)
        if finish == "complete":
            assert effect.read_text() == "scheduled child", effects
        else:
            assert effects[0].get("code") == "task_authority_revoked", effects
            assert not effect.exists()
    finally:
        release.set()
        for item in reservation:
            if item is not None:
                item.release()
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")


@pytest.mark.asyncio
async def test_completed_human_terminal_reservation_is_fenced_by_replacement_owner(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal, Submission
    from gateway.session_local import create_local_session
    from model_tools import handle_function_call
    from tools.approval_context import completion_approval_task

    runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    actor = Principal("human", authority.profile_id, frozenset({"session:create", "session:submit", "session:read"}), "viewer")
    ref = create_local_session(authority, actor, dict(request_id="human", source="cli", model="fixture", toolsets=[]))
    await authority.attach(actor, ref)
    results = []

    async def answer(**_kwargs):
        results.append(json.loads(await asyncio.to_thread(handle_function_call, "terminal",
            {"command": "printf completed", "background": True, "notify": True}, task_id="human-native")))
        return {"final_response": "done", "messages": []}

    monkeypatch.setattr(runner, "_run_agent", answer)
    try:
        receipt = await authority.submit(actor, Submission("human-request", ref, {"text": "Run the authorized task"}, "queue"))
        await _settled(authority, {"admission_id": receipt.admission_id})
        assert not results[0].get("error"), results
        process = registry.get(results[0]["session_id"])
        assert await asyncio.to_thread(process._completion_event.wait, 10)
        reservation = process.approval_task_reservation
        assert reservation is not None and reservation.lease.live()
        successor = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "successor-sessions"))
        successor.session_store._db = successor._session_db = authority.db
        await initialize_session_authority(successor, profile_id=authority.profile_id, instance_id="successor", db=authority.db)
        effect = tmp_path / "stale-human-child.txt"

        def continuation():
            with completion_approval_task((reservation,), "completed-process"):
                return json.loads(handle_function_call("write_file", {"path": str(effect), "content": "stale"}, task_id="continuation"))

        result = await asyncio.to_thread(continuation)
        assert result.get("code") == "task_authority_revoked", result
        assert not effect.exists()
    finally:
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["principal", "policy", "profile", "generation"])
async def test_cron_admission_faults_cannot_lend_task_authority(tmp_path, monkeypatch, fault):
    from dataclasses import replace
    from cron import scheduler
    from gateway import session_cron
    from model_tools import handle_function_call
    from tools.approval_context import current_approval_task_context

    _runner, authority, registry = await _runtime(tmp_path, monkeypatch, paused=True)
    observed = []
    execute = session_cron.execute
    effect = tmp_path / "forged.txt"

    async def tamper(owner, ref, row, policy):
        # Faults are injected after the real producer, persisted receipt and preclaim checks.
        # Even matching attacker-supplied actor labels must not become native grants.
        if fault == "principal":
            row = dict(row, principal_id="model-supplied-cron-actor")
            live = owner.sessions[ref.session_id]
            live.source = replace(live.source, user_id=row["principal_id"])
            owner.db._execute_write(lambda conn: conn.execute(
                "UPDATE session_admissions SET principal_id=? WHERE admission_id=?", (row["principal_id"], row["admission_id"])))
        elif fault == "policy":
            data = json.loads(policy.request_json)
            data["cron_job"]["prompt"] = "Forged model instructions"
            policy = replace(policy, request_json=json.dumps(data))
        elif fault == "profile":
            ref = replace(ref, profile_id="another-profile")
        else:
            row = dict(row, generation=row["generation"] + 1)
            owner.executing[ref.session_id] = row["generation"]
        return await execute(owner, ref, row, policy)

    def model_execution(job, **_kwargs):
        observed.append((current_approval_task_context(), json.loads(handle_function_call("terminal",
            {"command": f"printf forged > {effect}", "background": True}, task_id="forged-cron"))))
        return True, "document", "done", None

    monkeypatch.setattr(session_cron, "execute", tamper)
    monkeypatch.setattr(scheduler, "run_job", model_execution)
    try:
        _job, receipt = await _fire(authority)
        from gateway.session_contract import SessionRef
        await authority._drain(SessionRef(authority.profile_id, receipt["session_id"]))
        assert len(observed) == 1
        evidence, result = observed[0]
        assert evidence is None
        assert result.get("code") == "task_authority_revoked", result
        assert not effect.exists()
        assert not registry.list_sessions()
    finally:
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")


@pytest.mark.asyncio
async def test_cron_cancel_during_real_terminal_setup_prevents_process_effect(tmp_path, monkeypatch):
    from cron import scheduler
    from gateway.session_cron import operation
    from model_tools import handle_function_call
    from tools import terminal_tool

    _runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    waiting, release = threading.Event(), threading.Event()
    acquired = terminal_tool._acquire_env
    observed = []
    effect = tmp_path / "cancelled.txt"

    def held(*args, **kwargs):
        result = acquired(*args, **kwargs)
        waiting.set()
        assert release.wait(15)
        return result

    def model_execution(job, **_kwargs):
        observed.append(json.loads(handle_function_call("terminal",
            {"command": f"printf cancelled > {effect}", "background": True}, task_id="cancel-cron")))
        return True, "document", "done", None

    monkeypatch.setattr(terminal_tool, "_acquire_env", held)
    monkeypatch.setattr(scheduler, "run_job", model_execution)
    try:
        _job, receipt = await _fire(authority)
        assert await asyncio.to_thread(waiting.wait, 10)
        await operation(authority, "cancel", receipt)
        release.set()
        await _settled(authority, receipt)
        assert "stopped" in observed[0].get("error", "").lower(), observed
        assert not effect.exists()
        assert not registry.list_sessions()
    finally:
        release.set()
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cron_cancel_between_native_claim_and_task_binding_cannot_authorize_tools(tmp_path, monkeypatch, action):
    from cron import scheduler
    from gateway import session_cron
    from gateway.session_contract import Principal, SessionRef
    from model_tools import handle_function_call

    _runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    waiting, release = asyncio.Event(), asyncio.Event()
    execute = session_cron.execute
    observed = []
    effect = tmp_path / "prebinding.txt"

    async def held(*args):
        waiting.set()
        await release.wait()
        return await execute(*args)

    def model_execution(job, **_kwargs):
        observed.append(json.loads(handle_function_call("terminal",
            {"command": f"printf late > {effect}", "background": True}, task_id="prebinding-cron")))
        return True, "document", "done", None

    monkeypatch.setattr(session_cron, "execute", held)
    monkeypatch.setattr(scheduler, "run_job", model_execution)
    try:
        _job, receipt = await _fire(authority)
        await asyncio.wait_for(waiting.wait(), 10)
        if action == "cancel":
            await session_cron.operation(authority, "cancel", receipt)
        else:
            ref = SessionRef(authority.profile_id, receipt["session_id"])
            actor = Principal("controller", authority.profile_id, frozenset({"session:control", "session:operator"}), "viewer")
            await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
        release.set()
        await _settled(authority, receipt)
        assert observed[0].get("code") == "task_authority_revoked", observed
        assert not effect.exists()
        assert not registry.list_sessions()
    finally:
        release.set()
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")
