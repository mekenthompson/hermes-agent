"""Sync Relay LLM calls must proceed on a thread that already has a running loop.

Desktop tool dispatch can invoke the auxiliary approval guardian while an event
loop is running on that thread. ``asyncio.run`` is illegal there, and raising
turns every flagged command into an immediate escalate. The loop must also keep
running callbacks scheduled on it, or a prompt write queued on that loop never
flushes.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

pytest.importorskip("nemo_relay")

from agent import relay_llm, relay_runtime


def _provider_response(content: str) -> dict:
    return {
        "id": "aux-1",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
    }


@pytest.fixture()
def relay_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(),
        session_id="session-1",
        platform="desktop",
    )
    turn = relay_runtime.SESSION_COORDINATOR.begin_turn(lease, turn_id="turn-1", task_id="task-1")
    lease.host.retain_managed_execution("test.loop_thread_aux")
    try:
        yield lease.host
    finally:
        lease.host.release_managed_execution("test.loop_thread_aux")
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")
        relay_runtime.SESSION_COORDINATOR.release_conversation(lease)
        relay_runtime._reset_for_tests()


def test_sync_relay_llm_on_running_loop_returns_provider_result_and_lets_loop_callbacks_run(relay_turn):
    """The historic failure: unguarded sync execute on a live loop raises immediately."""
    ticks: list[str] = []
    provider_threads: list[int] = []
    loop_moved = threading.Event()

    def provider(request):
        provider_threads.append(threading.get_ident())
        assert loop_moved.wait(timeout=2), "loop stayed frozen during the aux call"
        return _provider_response("APPROVE")

    async def on_loop():
        loop = asyncio.get_running_loop()

        def tick():
            ticks.append("tick")
            if len(ticks) < 2:
                loop.call_soon(tick)
            else:
                loop_moved.set()

        loop.call_soon(tick)
        result = relay_llm.execute(
            {"messages": [{"role": "user", "content": "assess"}], "model": "m"},
            provider,
            session_id="session-1",
            name="aux",
            model_name="m",
            metadata={
                "api_mode": "chat_completions",
                "api_request_id": "req-aux",
                "call_role": "auxiliary:approval",
            },
        )
        return result

    result = asyncio.run(on_loop())
    assert result["choices"][0]["message"]["content"] == "APPROVE"
    assert ticks == ["tick", "tick"]
    assert provider_threads and provider_threads[0] != threading.main_thread().ident
