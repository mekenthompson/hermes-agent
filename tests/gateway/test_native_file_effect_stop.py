"""Native Stop fences file effects queued behind real file-tool resources."""

import asyncio
import json
import threading
from contextlib import contextmanager

import pytest


async def _native(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="owner")
    actor = Principal("owner", "default", frozenset({"session:create", "session:read", "session:control"}), "viewer")
    ref = create_local_session(authority, actor, dict(request_id="file-stop", source="cli", model="fixture", toolsets=[]))
    await authority.attach(actor, ref)
    return authority, actor, ref


def _request(kind, src, dst):
    if kind == "write":
        return "write_file", {"path": str(dst), "content": "after\n"}
    if kind == "replace":
        return "patch", {"mode": "replace", "path": str(src), "old_string": "before", "new_string": "after"}
    operation = {
        "add": f"*** Add File: {dst}\n+after",
        "delete": f"*** Delete File: {src}",
        "move": f"*** Move File: {src} -> {dst}",
    }[kind]
    return "patch", {"mode": "patch", "patch": f"*** Begin Patch\n{operation}\n*** End Patch"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["write", "replace", "add", "delete", "move"])
@pytest.mark.parametrize("stop", [False, True], ids=["live", "stopped"])
async def test_native_file_effect_waiting_for_path_lock(tmp_path, monkeypatch, kind, stop):
    from gateway.session_command_approval import command_task_scope
    from model_tools import handle_function_call
    from tools import file_state, file_tools
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations

    authority, actor, ref = await _native(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    src, dst = tmp_path / "before.txt", tmp_path / "after.txt"
    src.write_text("before\n")
    operations = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path), timeout=15, persistent=False))
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda _task_id="default": operations)
    original_lock = file_state.lock_path
    waiting = threading.Event()

    @contextmanager
    def observed_lock(path):
        waiting.set()
        with original_lock(path):
            yield

    monkeypatch.setattr(file_state, "lock_path", observed_lock)
    tool, arguments = _request(kind, src, dst)
    locked = dst if kind in {"write", "add", "move"} else src
    with command_task_scope(authority, ref, actor.subject, "/file-test", authority.route_epoch(route)):
        with original_lock(str(locked)):
            pending = asyncio.create_task(asyncio.to_thread(handle_function_call, tool, arguments, task_id="file-stop"))
            assert await asyncio.to_thread(waiting.wait, 10)
            if stop:
                await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
        result = json.loads(await asyncio.wait_for(pending, 15))
    if stop:
        assert result.get("code") == "task_authority_revoked", result
        assert src.read_text() == "before\n"
        assert not dst.exists()
    else:
        assert not result.get("error"), result
        if kind == "replace":
            assert src.read_text() == "after\n"
        elif kind in {"delete", "move"}:
            assert not src.exists()
        if kind in {"write", "add", "move"}:
            assert dst.read_text().rstrip("\n") == ("before" if kind == "move" else "after")


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True], ids=["live", "stopped"])
@pytest.mark.parametrize("native", [False, True], ids=["shell", "native"])
@pytest.mark.parametrize("kind", ["write", "replace", "delete", "move"])
async def test_native_stop_during_file_mutation_preflight(tmp_path, monkeypatch, stop, native, kind):
    from gateway.session_command_approval import command_task_scope
    from model_tools import handle_function_call
    from tools import file_operations, file_tools
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations

    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", "1" if native else "0")
    authority, actor, ref = await _native(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    src, dst = tmp_path / "before.txt", tmp_path / "after.txt"
    src.write_text("before\n")
    operations = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path), timeout=15, persistent=False))
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda _task_id="default": operations)
    waiting, release = threading.Event(), threading.Event()

    def hold_preflight(original):
        def held(*args, **kwargs):
            result = original(*args, **kwargs)
            waiting.set()
            assert release.wait(15)
            return result
        return held

    if kind in {"write", "replace"}:
        monkeypatch.setattr(operations, "_probe_write_target", hold_preflight(operations._probe_write_target))
    else:
        monkeypatch.setattr(file_operations, "get_write_denied_error", hold_preflight(file_operations.get_write_denied_error))
    tool, arguments = _request(kind, src, dst)
    try:
        with command_task_scope(authority, ref, actor.subject, "/file-test", authority.route_epoch(route)):
            pending = asyncio.create_task(asyncio.to_thread(handle_function_call, tool, arguments, task_id="file-stop"))
            assert await asyncio.to_thread(waiting.wait, 10)
            if stop:
                await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
            release.set()
            result = json.loads(await asyncio.wait_for(pending, 15))
        if stop:
            assert "stopped" in result.get("error", ""), result
            assert src.read_text() == "before\n"
            assert not dst.exists()
        else:
            assert not result.get("error"), result
            if kind == "replace":
                assert src.read_text() == "after\n"
            elif kind in {"delete", "move"}:
                assert not src.exists()
            if kind in {"write", "move"}:
                assert dst.read_text() == ("before\n" if kind == "move" else "after\n")
    finally:
        release.set()
