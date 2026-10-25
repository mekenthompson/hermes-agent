"""Stop at backend preparation/serialization cannot start a new file effect."""

import asyncio
import json
import threading

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["preparation", "spawn"])
@pytest.mark.parametrize("stop", [False, True], ids=["live", "stopped"])
async def test_native_stop_between_file_mutation_admission_and_backend_spawn(tmp_path, monkeypatch, boundary, stop):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_command_approval import command_task_scope
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from model_tools import handle_function_call
    from tools import file_tools
    from tools.environments import base
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", "0")
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="owner")
    actor = Principal("owner", "default", frozenset({"session:create", "session:read", "session:control"}), "viewer")
    ref = create_local_session(authority, actor, dict(request_id="spawn-stop", source="cli", model="fixture", toolsets=[]))
    await authority.attach(actor, ref)
    route = authority.sessions[ref.session_id].route
    dst = tmp_path / "after.txt"
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15, persistent=True)
    operations = ShellFileOperations(env)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda _task_id="default": operations)
    waiting, release = threading.Event(), threading.Event()
    calls = 0

    def held(original):
        def pause():
            nonlocal calls
            calls += 1
            # The first command probes; the second is the actual atomic write,
            # already past both registry and file-consumer authority checks.
            if calls == 2:
                waiting.set()
                assert release.wait(15)
            return original()
        return pause

    if boundary == "preparation":
        monkeypatch.setattr(env, "_before_execute", held(env._before_execute))
    else:
        monkeypatch.setattr(base, "_enter_foreground_spawn", held(base._enter_foreground_spawn))
    try:
        with command_task_scope(authority, ref, actor.subject, "/spawn-test", authority.route_epoch(route)):
            pending = asyncio.create_task(asyncio.to_thread(handle_function_call, "write_file",
                {"path": str(dst), "content": "after\n"}, task_id="spawn-stop"))
            assert await asyncio.to_thread(waiting.wait, 10)
            if boundary == "spawn":
                # Release preparation into the real common spawn serialization
                # lock while Stop revokes the native lease.
                with base._live_foreground_cond:
                    release.set()
                    if stop:
                        await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
            else:
                if stop:
                    await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
                release.set()
            result = json.loads(await asyncio.wait_for(pending, 15))
        if stop:
            assert "stopped" in result.get("error", ""), result
            assert not dst.exists()
            assert not list(tmp_path.glob(".hermes-tmp.*"))
        else:
            assert not result.get("error"), result
            assert dst.read_text() == "after\n"
        assert base._spawns_in_flight == 0
        assert not base._live_foreground
    finally:
        release.set()
