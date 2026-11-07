"""Sync approval assessment must proceed on a thread that already has a running loop.

A managed Relay attempt on that thread cannot call ``asyncio.run``. Raising
turns the guardian into an immediate escalate. The off-loop worker must keep
the caller's profile home; a bare thread would not.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("nemo_relay")

from agent import relay_runtime
from hermes_constants import get_hermes_home_override, reset_hermes_home_override, set_hermes_home_override


def _provider_response(content: str):
    return SimpleNamespace(
        id="aux-1",
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=None,
    )


def test_smart_approve_on_running_loop_returns_verdict_in_caller_profile(tmp_path, monkeypatch):
    """Enter through the guardian. The provider must see the caller profile, off the loop thread."""
    from agent import auxiliary_client
    from tools.approval_smart import _smart_approve

    caller_home = tmp_path / "caller-profile"
    caller_home.mkdir()
    token = set_hermes_home_override(str(caller_home))
    monkeypatch.setenv("HERMES_HOME", str(caller_home))
    relay_runtime._reset_for_tests()
    lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(),
        session_id="session-1",
        platform="desktop",
    )
    turn = relay_runtime.SESSION_COORDINATOR.begin_turn(lease, turn_id="turn-1", task_id="task-1")
    lease.host.retain_managed_execution("test.loop_thread_aux")
    seen: dict[str, object] = {}

    class _Completions:
        def create(self, **kwargs):
            seen["home"] = get_hermes_home_override()
            seen["thread"] = threading.get_ident()
            return _provider_response("APPROVE")

    class _Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=_Completions())
            self.base_url = "http://fixture.invalid/v1"

    monkeypatch.setattr(
        auxiliary_client,
        "_resolve_call_client",
        lambda *args, **kwargs: auxiliary_client._ResolvedAuxRoute(_Client(), "m", "fixture", "fixture"),
    )
    try:
        async def on_loop():
            return _smart_approve("python -c 'print(1)'", "script execution")

        verdict = asyncio.run(on_loop())
    finally:
        lease.host.release_managed_execution("test.loop_thread_aux")
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")
        relay_runtime.SESSION_COORDINATOR.release_conversation(lease)
        relay_runtime._reset_for_tests()
        reset_hermes_home_override(token)

    assert verdict == "approve"
    assert seen.get("home") == str(caller_home)
    assert seen.get("thread") != threading.main_thread().ident
