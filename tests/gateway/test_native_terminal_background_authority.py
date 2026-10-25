"""Real background terminal effects retain native authority through consumer waits."""

import asyncio
import json
import threading
from contextlib import ExitStack

import pytest


async def _runtime(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from hermes_state import SessionDB
    from tools import process_registry, terminal_tool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "home" / "sessions"))
    db = SessionDB(tmp_path / "home" / "state.db")
    runner.session_store._db = runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="owner", db=db)
    actor = Principal("owner", "default", frozenset({"session:create", "session:read", "session:control"}), "viewer")
    ref = create_local_session(authority, actor, dict(request_id="terminal-native", source="cli", model="fixture", toolsets=[]))
    await authority.attach(actor, ref)
    monkeypatch.setenv("HERMES_SESSION_KEY", authority.sessions[ref.session_id].route)
    config = terminal_tool._get_env_config
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {**config(), "env_type": "local", "cwd": str(tmp_path)})
    registry = process_registry.ProcessRegistry()
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    monkeypatch.setattr(registry, "_env_temp_dir", lambda _env: str(remote_dir))
    monkeypatch.setattr(process_registry, "process_registry", registry)
    return runner, authority, actor, ref, registry


def _hold_background_boundary(monkeypatch, boundary, registry, waiting, release):
    from tools import terminal_tool

    original_spawn = terminal_tool.spawn_background_process
    phase = threading.local()

    def spawn(**kwargs):
        phase.background = True
        if boundary == "remote":
            # Exercise the real remote shell protocol with the local test backend.
            kwargs["env_type"] = "fixture-remote"
        return original_spawn(**kwargs)

    def hold(original):
        def held(*args, **kwargs):
            result = original(*args, **kwargs)
            if boundary == "environment" or getattr(phase, "background", False):
                waiting.set()
                assert release.wait(15)
            return result
        return held

    monkeypatch.setattr(terminal_tool, "spawn_background_process", spawn)
    if boundary == "cwd":
        monkeypatch.setattr(terminal_tool, "_resolve_command_cwd", hold(terminal_tool._resolve_command_cwd))
    elif boundary == "environment":
        monkeypatch.setattr(terminal_tool, "_acquire_env", hold(terminal_tool._acquire_env))
    elif boundary == "remote":
        monkeypatch.setattr(registry, "_env_temp_dir", hold(registry._env_temp_dir))
    else:
        monkeypatch.setattr(registry, "_spawn_env", hold(registry._spawn_env))


@pytest.mark.asyncio
@pytest.mark.platforms("posix")
@pytest.mark.parametrize("boundary", ["environment", "cwd", "pipe", "pty", "remote"])
@pytest.mark.parametrize("finish", ["live", "stopped", "parent_complete"])
@pytest.mark.parametrize("notify", [False, True])
async def test_native_background_consumer_waits(tmp_path, monkeypatch, boundary, finish, notify):
    from gateway.session_command_approval import command_task_scope
    from model_tools import handle_function_call
    from tools.approval_context import completion_approval_task, current_approval_task_context, release_background_approval_task

    _runner, authority, actor, ref, registry = await _runtime(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    waiting, release = threading.Event(), threading.Event()
    _hold_background_boundary(monkeypatch, boundary, registry, waiting, release)
    effect = tmp_path / "effect.txt"
    try:
        with ExitStack() as scope:
            scope.enter_context(command_task_scope(authority, ref, actor.subject, "authorized work", authority.route_epoch(route)))
            pending = asyncio.create_task(asyncio.to_thread(handle_function_call, "terminal",
                {"command": f"printf after > {effect}", "background": True, "notify": notify,
                 "pty": boundary == "pty"}, task_id="terminal-native"))
            assert await asyncio.to_thread(waiting.wait, 10), pending.result() if pending.done() else "not admitted"
            if finish == "stopped":
                await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
            elif finish == "parent_complete":
                scope.close()
            release.set()
            receipt = json.loads(await asyncio.wait_for(pending, 20))
        if finish == "stopped":
            assert "stopped" in receipt.get("error", "").lower(), receipt
            assert not effect.exists(), receipt
            for entry in registry.list_sessions():
                assert entry["status"] == "exited" and entry["completion_reason"] == "failed_start"
                assert entry["pid"] is None and entry["exit_code"] is None
                assert not entry.get("reconciliation_required") and not entry.get("notify_on_complete")
        else:
            assert not receipt.get("error"), receipt
            session = registry.get(receipt["session_id"])
            assert await asyncio.to_thread(session._completion_event.wait, 10), receipt
            assert session.exit_code == 0, (receipt, session.output_buffer)
            assert effect.read_text() == "after"
            if boundary == "pty":
                assert session._pty is not None
            if notify:
                reservation = session.approval_task_reservation
                assert reservation is not None
                with completion_approval_task((reservation,), "terminal-completion"):
                    assert current_approval_task_context() is not None
                release_background_approval_task(reservation)
    finally:
        release.set()
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")


@pytest.mark.asyncio
async def test_real_plugin_admission_cannot_start_background_terminal_without_native_authority(tmp_path, monkeypatch):
    from model_tools import handle_function_call
    from tests.gateway.test_plugin_origin_injection_authority import _context, _publish
    from tests.waiting import async_wait_until
    from tools import terminal_tool
    from tools.approval_context import current_approval_task_context

    runner, authority, _actor, ref, registry = await _runtime(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    context, manager = _context(tmp_path / "home")
    _publish(runner, manager)
    receipts, acquired = [], []
    acquire = terminal_tool._acquire_env

    def observed_acquire(*args):
        acquired.append(True)
        return acquire(*args)

    monkeypatch.setattr(terminal_tool, "_acquire_env", observed_acquire)
    effect = tmp_path / "plugin-effect.txt"

    async def answer(**kwargs):
        assert current_approval_task_context() is None
        for arguments in ({"background": True}, {"background": True, "notify": True},
                          {"timeout": 9999}, {"command": f"printf after > {effect} &"}):
            receipts.append(json.loads(await asyncio.to_thread(handle_function_call, "terminal",
                {"command": f"printf after > {effect}", **arguments}, task_id="plugin-terminal")))
        return {"final_response": "checked", "messages": []}

    monkeypatch.setattr(runner, "_run_agent", answer)
    try:
        assert await asyncio.to_thread(context.inject_message, "continue work", session_key=route)
        await async_wait_until(lambda: len(receipts) == 4, timeout=15, what="plugin terminal refusals")
        assert all(receipt.get("code") == "task_authority_revoked" for receipt in receipts), receipts
        assert all("no valid native task authority" in receipt["error"] for receipt in receipts), receipts
        assert acquired == [], "missing authority must be diagnosed before environment setup"
        assert not effect.exists()
        assert not registry.list_sessions()
    finally:
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")


@pytest.mark.asyncio
async def test_stop_after_actual_local_spawn_retains_process_receipt(tmp_path, monkeypatch):
    from gateway.session_command_approval import command_task_scope
    from model_tools import handle_function_call
    from tests.waiting import async_wait_until

    _runner, authority, actor, ref, registry = await _runtime(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    waiting, release = threading.Event(), threading.Event()
    track = registry._track_started
    effect = tmp_path / "started.txt"

    def held(*args):
        waiting.set()
        assert release.wait(15)
        return track(*args)

    monkeypatch.setattr(registry, "_track_started", held)
    try:
        with command_task_scope(authority, ref, actor.subject, "authorized work", authority.route_epoch(route)):
            pending = asyncio.create_task(asyncio.to_thread(handle_function_call, "terminal",
                {"command": f"printf started > {effect}; sleep 30", "background": True, "notify": True},
                task_id="terminal-native"))
            assert await asyncio.to_thread(waiting.wait, 10)
            await async_wait_until(effect.exists, timeout=10, what="the actual process effect")
            await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
            release.set()
            receipt = json.loads(await asyncio.wait_for(pending, 15))
        session = registry.get(receipt["session_id"])
        assert session.pid == receipt["pid"]
        assert session.exited and session.process.poll() is not None, "Stop must terminate the captured local process"
        assert effect.read_text() == "started", "the effect before Stop remains part of the reconciliation history"
        assert receipt["code"] == "task_authority_revoked"
        assert receipt["reconciliation_required"]
        status = registry.poll(session.id)
        assert status["status"] == "exited" and status["reconciliation_required"]
    finally:
        release.set()
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")


@pytest.mark.asyncio
async def test_stopped_remote_reader_retains_unknown_state_through_checkpoint(tmp_path, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    from gateway.session_command_approval import command_task_scope
    from model_tools import handle_function_call
    from tests.waiting import async_wait_until
    from tools.process_registry import ProcessRegistry

    _runner, authority, actor, ref, registry = await _runtime(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    waiting, release = threading.Event(), threading.Event()
    poll = registry._env_poller_loop
    _hold_background_boundary(monkeypatch, "remote", registry, threading.Event(), _set_event())
    finish = tmp_path / "finish"
    effect = tmp_path / "started.txt"

    def held(*args):
        waiting.set()
        assert release.wait(15)
        return poll(*args)

    monkeypatch.setattr(registry, "_env_poller_loop", held)
    try:
        with command_task_scope(authority, ref, actor.subject, "authorized remote work", authority.route_epoch(route)):
            receipt = json.loads(await asyncio.to_thread(handle_function_call, "terminal",
                {"command": f"printf started > {effect}; while [ ! -e {finish} ]; do sleep 0.05; done",
                 "background": True, "notify": True}, task_id="terminal-native"))
            assert not receipt.get("error"), receipt
            session = registry.get(receipt["session_id"])
            assert await asyncio.to_thread(waiting.wait, 10)
            await async_wait_until(effect.exists, timeout=10, what="actual remote effect")
            with pytest.raises(RuntimeStoreError) as stopped:
                await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
            assert stopped.value.reason == "unknown_execution"
            assert not finish.exists() and not session.exited
            release.set()
        await async_wait_until(lambda: bool(session.reconciliation_required), timeout=10, what="remote uncertainty")
        assert not session.exited
        assert session.exit_code is None
        assert registry.poll(session.id)["status"] == "unknown"
        recovered = ProcessRegistry()
        assert recovered.recover_from_checkpoint() == 1
        assert recovered.poll(session.id)["status"] == "unknown"
        assert recovered.list_sessions()[0]["reconciliation_required"]

        def no_host_kill(*args):
            raise AssertionError("A sandbox PID is not a host-process identity")

        monkeypatch.setattr(recovered, "_terminate_host_pid", no_host_kill)
        assert recovered.kill_process(session.id)["status"] == "error"
        assert not recovered.get(session.id).exited
    finally:
        release.set()
        finish.touch()
        if "session" in locals():
            exit_path = tmp_path / "remote" / f"hermes_bg_{session.id}.exit"
            await async_wait_until(exit_path.exists, timeout=10, what="test remote process exit")
            registry._finish_exited(session, int(exit_path.read_text().strip()))
            await asyncio.to_thread(session._reader_thread.join, 5)
            assert not session._reader_thread.is_alive()


def _set_event():
    event = threading.Event()
    event.set()
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", ["exception", "missing_pid"])
async def test_remote_effect_before_missing_receipt_retains_unknown_session(tmp_path, monkeypatch, lost):
    from gateway.session_command_approval import command_task_scope
    from model_tools import handle_function_call
    from tests.waiting import async_wait_until
    from tools import terminal_tool
    from tools.process_registry import ProcessRegistry

    _runner, authority, actor, ref, registry = await _runtime(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    spawn = terminal_tool.spawn_background_process
    effect, finish = tmp_path / "effect.txt", tmp_path / "finish"
    launches, artifacts = [], []

    def remote_spawn(**kwargs):
        env = kwargs["env"]
        execute = env.execute

        def lose_response(command, **options):
            # Only the actual background launch crosses the ambiguous boundary.
            launches.append(command)
            assert any(item["reconciliation_required"] for item in registry.list_sessions())
            result = execute(command, **options)
            if lost == "exception":
                raise OSError("simulated lost launch response")
            return {**result, "output": ""}

        monkeypatch.setattr(env, "execute", lose_response)
        kwargs["env_type"] = "fixture-remote"
        result = spawn(**kwargs)
        monkeypatch.setattr(env, "execute", execute)
        return result

    monkeypatch.setattr(terminal_tool, "spawn_background_process", remote_spawn)
    try:
        with command_task_scope(authority, ref, actor.subject, "authorized remote work", authority.route_epoch(route)):
            receipt = json.loads(await asyncio.to_thread(handle_function_call, "terminal",
                {"command": f"printf once >> {effect}; while [ ! -e {finish} ]; do sleep 0.05; done",
                 "background": True, "notify": True}, task_id="terminal-native"))
        assert receipt["status"] == "unknown", receipt
        assert receipt["code"] == "process_launch_uncertain"
        assert receipt["pid"] is None
        session = registry.get(receipt["session_id"])
        assert not session.exited
        await async_wait_until(effect.exists, timeout=10, what="the effect without a receipt")
        assert effect.read_text() == "once"
        assert len(launches) == 1
        recovered = ProcessRegistry()
        assert recovered.recover_from_checkpoint() == 1
        assert recovered.poll(session.id)["status"] == "unknown"
        assert recovered.get(session.id).pid is None
        assert recovered.kill_process(session.id)["status"] == "error"
        assert not recovered.get(session.id).exited
        artifacts.append(tmp_path / "remote" / f"hermes_bg_{session.id}.exit")
    finally:
        finish.touch()
        for exit_path in artifacts:
            await async_wait_until(exit_path.exists, timeout=10, what="the launched test process to exit")


@pytest.mark.asyncio
@pytest.mark.parametrize("notify", [True, ["absent-pattern"]])
async def test_fast_completion_has_native_reservation_before_reader_publishes(tmp_path, monkeypatch, notify):
    from gateway.session_command_approval import command_task_scope
    from gateway.session_approval_completion import completion_reservation_for_event
    from model_tools import handle_function_call
    from tools.approval_context import completion_approval_task, current_approval_task_context, release_background_approval_task

    _runner, authority, actor, ref, registry = await _runtime(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    track = registry._track_started

    def wait_for_real_completion(session, *args):
        assert session.approval_task_reservation is not None
        track(session, *args)
        assert session._completion_event.wait(10)

    monkeypatch.setattr(registry, "_track_started", wait_for_real_completion)
    with command_task_scope(authority, ref, actor.subject, "authorized work", authority.route_epoch(route)):
        receipt = json.loads(await asyncio.to_thread(handle_function_call, "terminal",
            {"command": "printf finished", "background": True, "notify": notify}, task_id="terminal-native"))
    assert not receipt.get("error"), receipt
    if notify is not True:
        assert registry.get(receipt["session_id"]).approval_task_reservation is None
        assert registry.completion_queue.empty()
        return
    event = registry.completion_queue.get(timeout=5)
    reservation = completion_reservation_for_event(event, route, "default")
    assert reservation is not None, event
    with completion_approval_task((reservation,), "actual-fast-completion"):
        assert current_approval_task_context() is not None
    release_background_approval_task(reservation)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_watch_fallback_keeps_native_completion_scope_through_parent_close(tmp_path, monkeypatch, stop):
    from gateway.session_command_approval import command_task_scope
    from gateway.session_approval_completion import completion_reservation_for_event
    from model_tools import handle_function_call
    from tools.approval_context import completion_approval_task, current_approval_task_context, release_background_approval_task
    from tools.process_registry import WATCH_LIFETIME_MAX_HITS

    _runner, authority, actor, ref, registry = await _runtime(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    trigger = tmp_path / "trigger"
    try:
        with command_task_scope(authority, ref, actor.subject, "authorized watcher", authority.route_epoch(route)):
            receipt = json.loads(await asyncio.to_thread(handle_function_call, "terminal",
                {"command": f"while [ ! -e {trigger} ]; do sleep 0.05; done; printf READY",
                 "background": True, "notify": ["READY"]}, task_id="terminal-native"))
            assert not receipt.get("error"), receipt
            session = registry.get(receipt["session_id"])
            assert session.approval_task_reservation is not None
            session._watch_hits = WATCH_LIFETIME_MAX_HITS - 1
            if stop:
                await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
        trigger.touch()
        assert await asyncio.to_thread(session._completion_event.wait, 10)
        events = []
        while not registry.completion_queue.empty():
            events.append(registry.completion_queue.get_nowait())
        if stop:
            assert session.process.poll() is not None, "Stop must physically terminate this owned process"
            assert "READY" not in session.output_buffer
            assert not any(event.get("type") == "watch_match" for event in events), "a killed process cannot emit READY"
            for event in events:
                reservation = completion_reservation_for_event(event, route, "default")
                if reservation is not None:
                    with completion_approval_task((reservation,), "stopped-watch-completion"):
                        assert current_approval_task_context() is None
                    release_background_approval_task(reservation)
            return
        event = next(event for event in events if event["type"] == "completion")
        reservation = completion_reservation_for_event(event, route, "default")
        assert reservation is not None
        with completion_approval_task((reservation,), "watch-fallback-completion"):
            assert (current_approval_task_context() is not None) is (not stop)
        release_background_approval_task(reservation)
    finally:
        trigger.touch()
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_yielded_foreground_keeps_owner_without_manufacturing_authority(tmp_path, monkeypatch, native):
    from contextlib import nullcontext
    from gateway.session_command_approval import command_task_scope
    from model_tools import handle_function_call
    from tests.waiting import async_wait_until
    from tools import terminal_tool
    from tools.interrupt import request_yield
    from tools.approval_context import release_background_approval_task

    _runner, authority, actor, ref, registry = await _runtime(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    acquire, make_handler = terminal_tool._acquire_env, terminal_tool.yield_to_background_handler
    worker, handed = [], []
    observed = threading.Event()
    trigger, effect = tmp_path / "trigger", tmp_path / "effect"

    def identify(*args):
        worker.append(threading.get_ident())
        return acquire(*args)

    def observe_handler(**kwargs):
        handler = make_handler(**kwargs)

        def observed_handler(*args):
            result = handler(*args)
            handed.append(result)
            observed.set()
            return result
        return observed_handler

    monkeypatch.setattr(terminal_tool, "_acquire_env", identify)
    monkeypatch.setattr(terminal_tool, "yield_to_background_handler", observe_handler)
    try:
        scope = command_task_scope(authority, ref, actor.subject, "authorized work", authority.route_epoch(route)) if native else nullcontext()
        with scope:
            pending = asyncio.create_task(asyncio.to_thread(handle_function_call, "terminal",
                {"command": f"printf started > {effect}; while [ ! -e {trigger} ]; do sleep 0.05; done"},
                task_id="terminal-native"))
            await async_wait_until(effect.exists, timeout=10, what="foreground effect")
            request_yield(worker[0])
            assert await asyncio.to_thread(observed.wait, 10)
            if not native:
                assert handed == [None]
                assert registry.list_sessions() == []
            trigger.touch()
            receipt = json.loads(await asyncio.wait_for(pending, 15))
        if native:
            session = registry.get(receipt["session_id"])
            assert session.approval_task_reservation is not None
            assert await asyncio.to_thread(session._completion_event.wait, 10)
            release_background_approval_task(session.approval_task_reservation)
        else:
            assert "session_id" not in receipt
    finally:
        trigger.touch()
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_process_poll_promotion_requires_native_completion_scope(tmp_path, monkeypatch, native):
    from contextlib import nullcontext
    from gateway.session_command_approval import command_task_scope
    from model_tools import handle_function_call
    from tools.approval_context import completion_approval_task, current_approval_task_context, release_background_approval_task

    _runner, authority, actor, ref, registry = await _runtime(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    session = registry.spawn_local("sleep 30", cwd=str(tmp_path), task_id="terminal-native", session_key=route)
    try:
        scope = command_task_scope(authority, ref, actor.subject, "observe work", authority.route_epoch(route)) if native else nullcontext()
        with scope:
            for _ in range(2):
                receipt = json.loads(await asyncio.to_thread(handle_function_call, "process_manage",
                    {"action": "poll", "session_id": session.id}, task_id="terminal-native"))
        assert session.notify_on_complete is native, receipt
        if native:
            with completion_approval_task((session.approval_task_reservation,), "promoted-completion"):
                assert current_approval_task_context() is not None
            release_background_approval_task(session.approval_task_reservation)
    finally:
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")


@pytest.mark.asyncio
async def test_remote_signal_acknowledgement_does_not_claim_process_exit(tmp_path, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    from gateway.session_command_approval import command_task_scope
    from model_tools import handle_function_call
    from tests.waiting import async_wait_until

    _runner, authority, actor, ref, registry = await _runtime(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    _hold_background_boundary(monkeypatch, "remote", registry, threading.Event(), _set_event())
    effect, finish, completed = tmp_path / "started", tmp_path / "finish", tmp_path / "completed"
    try:
        with command_task_scope(authority, ref, actor.subject, "authorized remote work", authority.route_epoch(route)):
            receipt = json.loads(await asyncio.to_thread(handle_function_call, "terminal",
                {"command": f"printf started > {effect}; while [ ! -e {finish} ]; do sleep 0.05; done; printf done > {completed}",
                 "background": True, "notify": True}, task_id="terminal-native"))
            assert not receipt.get("error"), receipt
            session = registry.get(receipt["session_id"])
            await async_wait_until(effect.exists, timeout=10, what="remote process start")
            with pytest.raises(RuntimeStoreError) as stopped:
                await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
            assert stopped.value.reason == "unknown_execution"
            assert not completed.exists() and not session.exited
        await async_wait_until(lambda: bool(session.reconciliation_required), timeout=10, what="revoked remote reader")
        with command_task_scope(authority, ref, actor.subject, "stop the actual remote process", authority.route_epoch(route)):
            result = await asyncio.to_thread(registry.kill_process, session.id)
        assert result["status"] == "unknown", result
        assert result["reconciliation_required"]
        assert not session.exited
        assert session.exit_code is None
        assert registry.poll(session.id)["status"] == "unknown"
    finally:
        finish.touch()
        await async_wait_until(completed.exists, timeout=10, what="the actual remote child to finish")
        if "session" in locals():
            await asyncio.to_thread(session._reader_thread.join, 10)
            assert not session._reader_thread.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["after_kill", "exit_receipt"])
async def test_remote_inspection_failure_preserves_unknown_even_with_live_authority(tmp_path, monkeypatch, failure):
    from gateway.session_command_approval import command_task_scope
    from model_tools import handle_function_call
    from tests.waiting import async_wait_until
    from tools.process_registry import ProcessRegistry

    _runner, authority, actor, ref, registry = await _runtime(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    _hold_background_boundary(monkeypatch, "remote", registry, threading.Event(), _set_event())
    poll = registry._env_poller_loop
    waiting, release = threading.Event(), threading.Event()
    effect, finish, completed = tmp_path / "started", tmp_path / "finish", tmp_path / "completed"

    def held(*args):
        waiting.set()
        assert release.wait(15)
        return poll(*args)

    monkeypatch.setattr(registry, "_env_poller_loop", held)
    try:
        with command_task_scope(authority, ref, actor.subject, "authorized remote work", authority.route_epoch(route)):
            receipt = json.loads(await asyncio.to_thread(handle_function_call, "terminal",
                {"command": f"printf started > {effect}; while [ ! -e {finish} ]; do sleep 0.05; done; printf done > {completed}",
                 "background": True, "notify": True}, task_id="terminal-native"))
            assert not receipt.get("error"), receipt
            session = registry.get(receipt["session_id"])
            assert await asyncio.to_thread(waiting.wait, 10)
            await async_wait_until(effect.exists, timeout=10, what="actual remote start")
            execute = session.env_ref.execute

            def failed_inspection(command, **kwargs):
                if failure == "after_kill":
                    raise OSError("simulated remote inspection disconnect")
                result = execute(command, **kwargs)
                return {**result, "output": "unavailable"} if command.startswith("cat ") and ".exit" in command else result

            if failure == "after_kill":
                result = await asyncio.to_thread(registry.kill_process, session.id)
                assert result["status"] == "unknown", result
                assert not completed.exists(), "signal acknowledgement did not stop the actual child"
            else:
                finish.touch()
                exit_path = tmp_path / "remote" / f"hermes_bg_{session.id}.exit"
                await async_wait_until(exit_path.exists, timeout=10, what="actual exit receipt")
            monkeypatch.setattr(session.env_ref, "execute", failed_inspection)
            release.set()
        await asyncio.to_thread(session._reader_thread.join, 10)
        assert not session._reader_thread.is_alive()
        assert session.reconciliation_required
        assert "inspection failed" in session.reconciliation_required
        assert not session.exited
        assert session.exit_code is None
        assert registry.poll(session.id)["status"] == "unknown"
        recovered = ProcessRegistry()
        assert recovered.recover_from_checkpoint() == 1
        assert recovered.poll(session.id)["status"] == "unknown"
    finally:
        release.set()
        finish.touch()
        await async_wait_until(completed.exists, timeout=10, what="the actual remote child to finish")


@pytest.mark.asyncio
@pytest.mark.platforms("posix")
async def test_healthy_remote_process_survives_abrupt_owner_restart_as_unknown(tmp_path, monkeypatch):
    import subprocess
    import sys
    from pathlib import Path
    from tests.waiting import async_wait_until
    from tools.process_registry import ProcessRegistry

    source = r'''
import asyncio, json, os, sys, threading
from pathlib import Path
from pytest import MonkeyPatch
from tests.gateway.test_native_terminal_background_authority import _runtime, _hold_background_boundary, _set_event
from gateway.session_command_approval import command_task_scope
from model_tools import handle_function_call

async def launch():
    home = Path(sys.argv[1])
    patch = MonkeyPatch()
    _, authority, actor, ref, registry = await _runtime(home, patch)
    route = authority.sessions[ref.session_id].route
    _hold_background_boundary(patch, "remote", registry, threading.Event(), _set_event())
    started, finish, completed = home / "started", home / "finish", home / "completed"
    with command_task_scope(authority, ref, actor.subject, "authorized remote work", authority.route_epoch(route)):
        receipt = json.loads(await asyncio.to_thread(handle_function_call, "terminal",
            {"command": f"printf started > {started}; while [ ! -e {finish} ]; do sleep 0.05; done; printf done > {completed}",
             "background": True, "notify": True}, task_id="terminal-restart"))
        assert not receipt.get("error"), receipt
        session = registry.get(receipt["session_id"])
        assert session.pid and not session.reconciliation_required
        assert session.approval_task_reservation is not None
        (home / "receipt.json").write_text(json.dumps(receipt))
        os._exit(19)

asyncio.run(launch())
'''
    finish, completed = tmp_path / "finish", tmp_path / "completed"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    try:
        child = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", source, str(tmp_path)],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, timeout=30, check=False)
        assert child.returncode == 19, child.stderr.decode()
        receipt = json.loads((tmp_path / "receipt.json").read_text())
        await async_wait_until((tmp_path / "started").exists, timeout=10, what="surviving remote process")
        recovered = ProcessRegistry()
        assert recovered.recover_from_checkpoint() == 1
        session = recovered.get(receipt["session_id"])
        assert session.pid == receipt["pid"]
        assert session.pid_scope == "sandbox"
        assert session.detached and not session.exited
        assert session.env_ref is None
        assert getattr(session, "approval_task_reservation", None) is None
        assert recovered.poll(session.id)["status"] == "unknown"
        assert recovered.kill_process(session.id)["status"] == "error"
        assert not session.exited
    finally:
        finish.touch()
        await async_wait_until(completed.exists, timeout=10, what="surviving test process to finish")


@pytest.mark.asyncio
async def test_native_timeout_promotion_and_foreground_control(tmp_path, monkeypatch):
    from gateway.session_command_approval import command_task_scope
    from model_tools import handle_function_call
    from tools.approval_context import release_background_approval_task

    _runner, authority, actor, ref, registry = await _runtime(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    try:
        with command_task_scope(authority, ref, actor.subject, "bounded work", authority.route_epoch(route)):
            foreground = json.loads(await asyncio.to_thread(handle_function_call, "terminal",
                {"command": "printf foreground"}, task_id="terminal-native"))
            promoted = json.loads(await asyncio.to_thread(handle_function_call, "terminal",
                {"command": "printf background", "timeout": 9999}, task_id="terminal-native"))
        assert foreground["output"] == "foreground", foreground
        assert not promoted.get("error"), promoted
        assert promoted["promoted_from_foreground"]
        session = registry.get(promoted["session_id"])
        assert await asyncio.to_thread(session._completion_event.wait, 10)
        assert session.exit_code == 0
        assert "background" in session.output_buffer
        assert session.approval_task_reservation is not None
        release_background_approval_task(session.approval_task_reservation)
    finally:
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")


@pytest.mark.platforms("posix")
@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_lost", [True, False])
async def test_remote_launch_survives_another_interpreter_checkpoint(tmp_path, monkeypatch, receipt_lost):
    """A worker's empty registry cannot erase another worker's external effect."""
    from pathlib import Path
    import subprocess
    import sys

    from gateway.session_command_approval import command_task_scope
    from model_tools import handle_function_call
    from tools import terminal_tool
    from tools.process_registry import ProcessRegistry
    from tests.waiting import async_wait_until

    _runner, authority, actor, ref, registry = await _runtime(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    spawn = terminal_tool.spawn_background_process
    effect, finish, completed = tmp_path / "started", tmp_path / "finish", tmp_path / "completed"

    def remote_spawn(**kwargs):
        env = kwargs["env"]
        execute = env.execute

        def lose_response(command, **options):
            execute(command, **options)
            raise OSError("lost remote launch response")

        if receipt_lost:
            monkeypatch.setattr(env, "execute", lose_response)
        kwargs["env_type"] = "fixture-remote"
        try:
            return spawn(**kwargs)
        finally:
            monkeypatch.setattr(env, "execute", execute)

    monkeypatch.setattr(terminal_tool, "spawn_background_process", remote_spawn)
    try:
        with command_task_scope(authority, ref, actor.subject, "authorized remote work", authority.route_epoch(route)):
            receipt = json.loads(await asyncio.to_thread(handle_function_call, "terminal", {
                "command": f"printf started > {effect}; while [ ! -e {finish} ]; do sleep 0.05; done; printf done > {completed}",
                "background": True, "notify": True}, task_id="remote-worker"))
        assert not receipt.get("error"), receipt
        assert (receipt.get("status") == "unknown") is receipt_lost, receipt
        await async_wait_until(effect.exists, timeout=10, what="actual remote launch effect")
        other = await asyncio.to_thread(subprocess.run, [sys.executable, "-c",
            "from tools.process_registry import ProcessRegistry; ProcessRegistry()._write_checkpoint()"],
            cwd=str(Path(__file__).resolve().parents[2]), capture_output=True, timeout=20, check=False)
        assert other.returncode == 0, other.stderr.decode()
        restarted = ProcessRegistry()
        assert restarted.recover_from_checkpoint() == 1
        session = restarted.get(receipt["session_id"])
        assert session.pid_scope == "sandbox" and session.env_ref is None
        assert restarted.poll(session.id)["status"] == "unknown"
        # Repeated unrelated writes and restarts also preserve the same fenced record.
        other = await asyncio.to_thread(subprocess.run, [sys.executable, "-c",
            "from tools.process_registry import ProcessRegistry; ProcessRegistry()._write_checkpoint()"],
            cwd=str(Path(__file__).resolve().parents[2]), capture_output=True, timeout=20, check=False)
        assert other.returncode == 0, other.stderr.decode()
        assert ProcessRegistry().recover_from_checkpoint() == 1
    finally:
        finish.touch()
        await async_wait_until(completed.exists, timeout=10, what="test remote process exit")
        if not receipt_lost:
            assert await asyncio.to_thread(registry.get(receipt["session_id"])._completion_event.wait, 10)
